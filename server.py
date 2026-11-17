import argparse
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from analysis import Metrics
from protocol import CHUNK, VERSION, ProtocolError, receive_message, send_message, sha256_file
from security import password_record, storage_path, verify_password

LOG = logging.getLogger('server')

class RequestError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def text_field(message, field, default='', limit=500):
    value = message.get(field, default)
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise RequestError('BAD_REQUEST', f'Invalid {field}')
    return value


def integer(message, key, default=0, maximum=10 * 1024**3):
    value = message.get(key, default)
    if type(value) is not int or not 0 <= value <= maximum:
        raise RequestError('BAD_REQUEST', f'Invalid {key}')
    return value


class FileServer:
    def __init__(self, host='0.0.0.0', port=5000, storage='storage', cert='certs/server.crt',
                 key='certs/server.key', users='users.json', log='logs/server.csv',
                 timeout=120, max_clients=32, max_file_bytes=10 * 1024**3, send_delay=0):
        self.host, self.port = host, port
        self.root = Path(storage).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.users = json.loads(Path(users).read_text(encoding='utf-8'))
        if not isinstance(self.users, dict) or not self.users:
            raise ValueError('Create at least one account with setup.py user')
        self.dummy = password_record(uuid.uuid4().hex)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(cert, key)
        self.timeout, self.max_file_bytes, self.send_delay = timeout, max_file_bytes, send_delay
        self.guard = threading.RLock()
        self.busy = set()
        self.sessions, self.threads = set(), set()
        self.auth_attempts = defaultdict(deque)
        self.slots = threading.BoundedSemaphore(max_clients)
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.listener = None
        self.metrics = Metrics(log)

    @contextmanager
    def reserve(self, path):
        with self.guard:
            if any(path == p or path in p.parents or p in path.parents for p in self.busy):
                raise RequestError('BUSY', 'This path is currently being processed')
            self.busy.add(path)
        try:
            yield
        finally:
            with self.guard:
                self.busy.remove(path)

    def resolve(self, name, allow_root=False):
        try:
            return storage_path(self.root, name, allow_root)
        except ValueError as exc:
            raise RequestError('INVALID_PATH', str(exc)) from exc

    def serve_forever(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                self.listener = listener
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.host, self.port))
                self.port = listener.getsockname()[1]
                listener.listen(32)
                listener.settimeout(.5)
                self.ready.set()
                LOG.info('READY %s:%s | storage=%s | analysis=%s', self.host, self.port, self.root, self.metrics.path)
                while not self.stopping.is_set():
                    try:
                        raw, address = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        if self.stopping.is_set():
                            break
                        raise
                    if not self.slots.acquire(blocking=False):
                        raw.close()
                        continue
                    thread = threading.Thread(target=self._worker, args=(raw, address), daemon=True)
                    with self.guard:
                        self.threads.add(thread)
                    thread.start()
        finally:
            self.shutdown()
            with self.guard:
                workers = list(self.threads)
            for worker in workers:
                worker.join()
            self.metrics.close()

    def shutdown(self):
        self.stopping.set()
        with self.guard:
            sessions = list(self.sessions)
        for conn in sessions:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def _worker(self, raw, address):
        conn = raw
        session = uuid.uuid4().hex
        try:
            raw.settimeout(min(self.timeout, 15))
            with self.guard:
                self.sessions.add(raw)
            conn = self.context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            with self.guard:
                self.sessions.discard(raw)
                self.sessions.add(conn)
            conn.do_handshake()
            conn.settimeout(self.timeout)
            auth = receive_message(conn)
            with self.guard:
                attempts = self.auth_attempts[address[0]]
                now = time.monotonic()
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                limited = len(attempts) >= 10
            user = auth.get('username', '')
            password = auth.get('password', '')
            valid_shape = (auth.get('op') == 'auth' and auth.get('version') == VERSION
                           and isinstance(user, str) and len(user) <= 64
                           and isinstance(password, str) and len(password) <= 1024)
            valid = False
            if valid_shape and not limited:
                valid = verify_password(password, self.users.get(user, self.dummy)) and user in self.users
            if not valid:
                with self.guard:
                    if not limited:
                        self.auth_attempts[address[0]].append(time.monotonic())
                send_message(conn, {'status': 'error', 'code': 'AUTH_FAILED',
                                    'message': 'Authentication failed or rate limit reached'})
                return
            send_message(conn, {'status': 'ok', 'session_id': session, 'version': VERSION})
            LOG.info('Connected user=%s peer=%s session=%s', user, address[0], session)
            while not self.stopping.is_set():
                request = receive_message(conn)
                if not self.handle(conn, request, user, session):
                    break
        except (OSError, EOFError, ProtocolError, ValueError) as exc:
            LOG.info('Connection ended peer=%s reason=%s', address[0], type(exc).__name__)
        except Exception:
            LOG.exception('Unexpected worker failure')
        finally:
            with self.guard:
                self.sessions.discard(raw)
                self.sessions.discard(conn)
                self.threads.discard(threading.current_thread())
            conn.close()
            self.slots.release()

    def handle(self, conn, req, user, session):
        start = time.perf_counter()
        row = dict(timestamp_utc=datetime.now(timezone.utc).isoformat(), side='server',
                   session_id=session, request_id=uuid.uuid4().hex, client=user,
                   operation='invalid', path='', bytes=0, file_bytes=0, offset=0,
                   transfer_s=0, response_s=0, status='error', error='')
        with self.guard:
            row['active_clients'] = len(self.sessions)
        responded = False
        def reply(status='ok', **kwargs):
            nonlocal responded
            if not responded:
                row['response_s'] = time.perf_counter() - start
                responded = True
            send_message(conn, {'status': status, **kwargs})
        try:
            row['request_id'] = text_field(req, 'id', limit=64)
            if not row['request_id']:
                raise RequestError('BAD_REQUEST', 'Request id is required')
            op = row['operation'] = text_field(req, 'op', limit=32)
            row['trial'] = text_field(req, 'trial', limit=80)
            row['load'] = integer(req, 'load', default=1, maximum=32)
            row['path'] = name = text_field(req, 'path')
            if op == 'disconnect':
                reply(); row['status'] = 'ok'; return False
            if op not in ('dir', 'mkdir', 'rmdir', 'delete', 'upload', 'download'):
                raise RequestError('BAD_REQUEST', 'Unknown operation')
            with self.guard:
                path = self.resolve(name, allow_root=op == 'dir')
            if op == 'dir':
                with self.guard:
                    if not path.is_dir():
                        raise RequestError('NOT_FOUND', 'Directory does not exist')
                    entries = []
                    for child in sorted(path.iterdir(), key=lambda p: p.name.casefold()):
                        if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
                            continue
                        entries.append({'name': child.name, 'type': 'dir' if child.is_dir() else 'file',
                                        'bytes': child.stat().st_size if child.is_file() else 0})
                        if len(entries) > 2000:
                            raise RequestError('TOO_MANY_ENTRIES', 'List smaller subdirectories; limit is 2000 entries')
                reply(entries=entries)
            else:
                with self.reserve(path):
                    if op == 'upload':
                        self.upload(conn, req, path, row, reply)
                    elif op == 'download':
                        self.download(conn, req, path, row, reply)
                    else:
                        with self.guard:
                            if op == 'mkdir':
                                path.mkdir()  
                            elif op == 'rmdir':
                                if not path.is_dir():
                                    raise RequestError('NOT_FOUND', 'Directory does not exist')
                                if any(path.iterdir()):
                                    raise RequestError('NOT_EMPTY', 'Only empty directories may be deleted')
                                path.rmdir()
                            else:
                                if not path.is_file():
                                    raise RequestError('NOT_FOUND', 'File does not exist')
                                path.unlink()
                        reply()
            if row['status'] != 'cancelled':
                row['status'] = 'ok'
        except (RequestError, FileNotFoundError, FileExistsError, IsADirectoryError, NotADirectoryError, PermissionError) as exc:
            if row.get('_binary'):
                row['error'] = 'CONNECTION_OR_IO_ERROR'
                raise  
            codes = {FileNotFoundError: 'NOT_FOUND', FileExistsError: 'EXISTS', IsADirectoryError: 'NOT_FILE',
                     NotADirectoryError: 'NOT_DIRECTORY', PermissionError: 'ACCESS_DENIED'}
            row['error'] = getattr(exc, 'code', codes.get(type(exc), 'IO_ERROR'))
            reply('error', code=row['error'], message=str(exc) if isinstance(exc, RequestError) else row['error'])
        except (OSError, EOFError, ProtocolError):
            row['error'] = 'CONNECTION_OR_IO_ERROR'
            raise
        finally:
            row['total_s'] = time.perf_counter() - start
            row['mb_s'] = row['bytes'] / 1_000_000 / row['transfer_s'] if row['transfer_s'] else 0
            self.metrics.record(**row)
        return True

    def upload(self, conn, req, path, row, reply):
        size = integer(req, 'size', maximum=self.max_file_bytes)
        digest = text_field(req, 'sha256', limit=64)
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise RequestError('BAD_REQUEST', 'SHA-256 is required')
        row['file_bytes'] = size
        if not path.parent.is_dir():
            raise RequestError('NOT_FOUND', 'Destination parent directory does not exist')
        if path.exists():
            if not path.is_file():
                raise RequestError('NOT_FILE', 'Destination is a directory')
            reply('exists', message='File exists. Overwrite?')
            decision = receive_message(conn)
            if decision.get('overwrite') is not True:
                row['status'] = 'cancelled'
                reply('cancelled')
                return
        fd, temporary = tempfile.mkstemp(prefix='cnt-upload-', dir=self.root.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                row['_binary'] = True
                reply('ready')
                begin = time.perf_counter()
                hasher = hashlib.sha256()
                try:
                    while row['bytes'] < size:
                        chunk = conn.recv(min(CHUNK, size - row['bytes']))
                        if not chunk:
                            raise EOFError('Upload interrupted')
                        out.write(chunk); hasher.update(chunk)
                        row['bytes'] += len(chunk)
                    out.flush(); os.fsync(out.fileno())
                    row['_binary'] = False
                finally:
                    row['transfer_s'] = time.perf_counter() - begin
            if hasher.hexdigest() != digest:
                raise RequestError('HASH_MISMATCH', 'Upload digest did not match; original file preserved')
            with self.guard:
                os.replace(temporary, path)
            reply('ok', sha256=digest, bytes=size)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def download(self, conn, req, path, row, reply):
        if not path.is_file():
            raise RequestError('NOT_FOUND', 'File does not exist')
        size = path.stat().st_size
        offset = integer(req, 'offset', maximum=size)
        digest = sha256_file(path)
        if offset and req.get('sha256') != digest:
            raise RequestError('CHANGED', 'Remote file changed; restart into a fresh local destination')
        row.update(file_bytes=size, offset=offset)
        # Client can decline before binary data begins (e.g. local file conflicts).
        reply('ready', size=size, offset=offset, sha256=digest)
        if receive_message(conn).get('accept') is not True:
            row['status'] = 'cancelled'; reply('cancelled'); return
        begin = time.perf_counter()
        row['_binary'] = True
        try:
            with path.open('rb') as source:
                source.seek(offset)
                remaining = size - offset
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise EOFError('Server file was externally modified')
                    conn.sendall(chunk)
                    row['bytes'] += len(chunk); remaining -= len(chunk)
                    if self.send_delay:
                        time.sleep(self.send_delay)
        finally:
            row['transfer_s'] = time.perf_counter() - begin
        row['_binary'] = False
        ack = receive_message(conn)
        if ack.get('verified') is not True:
            raise RequestError('HASH_MISMATCH', 'Client could not verify the downloaded file')
        reply('ok', bytes=row['bytes'], sha256=digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0'); parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--storage', default='storage'); parser.add_argument('--users', default='users.json')
    parser.add_argument('--cert', default='certs/server.crt'); parser.add_argument('--key', default='certs/server.key')
    parser.add_argument('--log', default='logs/server.csv'); parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--max-clients', type=int, default=32)
    parser.add_argument('--max-file-bytes', type=int, default=10 * 1024**3)
    parser.add_argument('--send-delay', type=float, default=0, help='Seconds per chunk for recovery demo only; use 0 for benchmarks')
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_clients < 1 or args.max_file_bytes < 0 or args.send_delay < 0:
        parser.error('Timeout and client limit must be positive; size/delay cannot be negative')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = FileServer(**vars(args))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()

