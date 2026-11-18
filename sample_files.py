import argparse
import json
import wave
from pathlib import Path
from protocol import CHUNK, sha256_file


def inspect(path):
    path = Path(path)
    print(json.dumps({'name': path.name, 'bytes': path.stat().st_size,
                      'sha256': sha256_file(path)}, indent=2))


def generate(kind, path, mib):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = mib * 1024 * 1024
    with path.open('xb') as handle:
        if kind == 'text':
            line = b'CNT3004 Group 8 network file transfer test.\n'
            chunk = (line * (CHUNK // len(line) + 1))[:CHUNK]
            remaining = size
            while remaining:
                part = chunk[:min(remaining, len(chunk))]
                handle.write(part); remaining -= len(part)
        else:
            # Valid silent 16-bit stereo PCM audio, not arbitrary renamed bytes.
            with wave.open(handle, 'wb') as audio:
                audio.setnchannels(2); audio.setsampwidth(2); audio.setframerate(48000)
                remaining = size - size % 4
                chunk = b'\x00' * CHUNK
                while remaining:
                    part = chunk[:min(remaining, CHUNK)]
                    audio.writeframesraw(part); remaining -= len(part)
    inspect(path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, default in [('text', 25), ('wav', 1024)]:
        item = sub.add_parser(name); item.add_argument('path')
        item.add_argument('--mib', type=int, default=default)
    item = sub.add_parser('inspect'); item.add_argument('path')
    args = parser.parse_args()
    if args.command == 'inspect':
        inspect(args.path)
    else:
        if not 1 <= args.mib <= 3000:
            parser.error('Use a MiB size between 1 and 3000')
        generate(args.command, args.path, args.mib)
