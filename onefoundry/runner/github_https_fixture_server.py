"""Fixed synthetic GitHub-shaped HTTPS server for the opt-in Compose proof."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import stat
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DUMMY_TOKEN = "onefoundry-fixture-dummy-token-v2"
COMMIT = "a" * 40
DATA = b"synthetic HTTPS fixture only\n"


def _git_hash(kind: str, value: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(value)}\0".encode() + value).hexdigest()  # noqa: S324


BLOB = _git_hash("blob", DATA)
TREE = _git_hash("tree", b"100644 file.txt\0" + bytes.fromhex(BLOB))
ROOT = "/repos/sample/project/git"
MATERIAL = {"server-key.pem", "ca.pem", "dummy-token"}


def _existing_material(root: Path, now: datetime) -> bool:
    names = {entry.name for entry in root.iterdir()}
    if not names:
        return False
    if names != MATERIAL:
        raise ValueError("fixture material is incomplete or contains unexpected files")
    for name, mode in (("server-key.pem", 0o600), ("ca.pem", 0o644), ("dummy-token", 0o600)):
        info = (root / name).lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError("fixture material has an unsafe file type or mode")
    if (root / "dummy-token").read_text() != DUMMY_TOKEN + "\n":
        raise ValueError("fixture dummy token changed")
    try:
        certificate = x509.load_pem_x509_certificate((root / "ca.pem").read_bytes())
        key = serialization.load_pem_private_key((root / "server-key.pem").read_bytes(), password=None)
        if (
            certificate.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            != key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            or certificate.subject != x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            or certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
            != ["localhost"]
            or certificate.not_valid_before_utc > now
        ):
            raise ValueError("fixture TLS material is invalid")
    except (ValueError, TypeError, x509.ExtensionNotFound):
        raise ValueError("fixture TLS material is invalid") from None
    return certificate.not_valid_after_utc > now + timedelta(hours=1)


def _write_material(root: Path, name: str, data: bytes, mode: int) -> None:
    temporary = root / f".{name}.{uuid4()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / name)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare(root: Path) -> None:
    """Reuse valid fixture material or renew it inside the dedicated fixture volume."""
    if root.is_symlink():
        raise ValueError("fixture root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    valid = _existing_material(root, now)
    if valid:
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    _write_material(
        root,
        "server-key.pem",
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        0o600,
    )
    _write_material(root, "ca.pem", cert.public_bytes(serialization.Encoding.PEM), 0o644)
    if not (root / "dummy-token").exists():
        _write_material(root, "dummy-token", (DUMMY_TOKEN + "\n").encode(), 0o600)


def serve(root: Path) -> None:
    token = (root / "dummy-token").read_text().strip()
    paths: dict[str, object] = {
        ROOT + "/ref/heads/main": {"ref": "refs/heads/main", "object": {"sha": COMMIT}},
        ROOT + "/commits/" + COMMIT: {"sha": COMMIT, "tree": {"sha": TREE}},
        ROOT + "/trees/" + TREE: {
            "sha": TREE,
            "truncated": False,
            "tree": [{"path": "file.txt", "mode": "100644", "type": "blob", "sha": BLOB}],
        },
        ROOT + "/blobs/" + BLOB: {
            "sha": BLOB,
            "encoding": "base64",
            "content": base64.b64encode(DATA).decode(),
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(401)
                return
            value = paths.get(self.path)
            if value is None:
                self.send_error(404)
                return
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(root / "ca.pem"), str(root / "server-key.pem"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.root)
        print(json.dumps({"kind": "synthetic_https_fixture_v2", "revision": COMMIT, "tree": TREE}))
    else:
        serve(args.root)


if __name__ == "__main__":
    main()
