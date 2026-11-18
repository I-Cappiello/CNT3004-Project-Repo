import hashlib
import json
import struct

CHUNK = 1024 * 1024
MAX_HEADER = 1024 * 1024
VERSION = 1

class ProtocolError(Exception):
    pass

class RemoteError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')


def receive_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(min(CHUNK, count - len(data)))
        if not part:
            raise EOFError('Connection closed before the expected bytes arrived')
        data.extend(part)
    return bytes(data)


def send_message(sock, message):
    raw = json.dumps(message, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_HEADER:
        raise ProtocolError('Control message exceeds size limit')
    sock.sendall(struct.pack('!I', len(raw)) + raw)


def receive_message(sock):
    size = struct.unpack('!I', receive_exact(sock, 4))[0]
    if not 0 < size <= MAX_HEADER:
        raise ProtocolError('Invalid control message length')
    try:
        value = json.loads(receive_exact(sock, size).decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError('Invalid JSON control message') from exc
    if not isinstance(value, dict):
        raise ProtocolError('Control message must be an object')
    return value


def check(message):
    if message.get('status') == 'error':
        raise RemoteError(message.get('code', 'ERROR'), message.get('message', 'Request failed'))
    return message


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()
