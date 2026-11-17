"""Create local TLS certificates and password-hashed accounts."""
import argparse
import getpass
import ipaddress
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from security import password_record


def certificates(ips, folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = ['ca.crt', 'server.crt', 'server.key']
    if any((folder / name).exists() for name in names):
        raise FileExistsError('Certificate files exist. Use a fresh --out directory to rotate certificates')
    addresses = [ipaddress.ip_address(value) for value in ips]
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    now = datetime.now(timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CNT3004 Group 8 Local CA')])
    def base(subject, issuer, public_key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)))
    ca = (base(ca_name, ca_name, ca_key.public_key())
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CNT3004 File Server')])
    cert = (base(subject, ca_name, key.public_key())
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip) for ip in addresses]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (folder / 'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / 'server.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(folder / 'server.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(private)
    # CA private key is intentionally not persisted. Rotation creates a new CA.
    print('Created certificates for:', ', '.join(ips))
    print('Copy ONLY ca.crt to clients. Keep server.key private. CA SHA-256 fingerprint:')
    print(ca.fingerprint(hashes.SHA256()).hex())


def add_user(username, password, filename, replace=False):
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}', username):
        raise ValueError('Username must contain 1–64 letters, digits, underscores or hyphens')
    if not 12 <= len(password) <= 1024:
        raise ValueError('Use a password of 12–1024 characters')
    path = Path(filename)
    users = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if username in users and not replace:
        raise ValueError('Account exists. Use --replace to change its password')
    users[username] = password_record(password)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(users, handle, indent=2)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    print(f'Saved account {username}. Restart server to load account changes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cert = sub.add_parser('cert'); cert.add_argument('--ip', action='append', required=True)
    cert.add_argument('--out', default='certs')
    user = sub.add_parser('user'); user.add_argument('username'); user.add_argument('--file', default='users.json')
    user.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'cert':
            certificates(args.ip, args.out)
        else:
            password = getpass.getpass('New password (at least 12 characters): ')
            if password != getpass.getpass('Repeat password: '):
                raise ValueError('Passwords do not match')
            add_user(args.username, password, args.file, args.replace)
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f'Error: {exc}\n')

if __name__ == '__main__':
    main()
