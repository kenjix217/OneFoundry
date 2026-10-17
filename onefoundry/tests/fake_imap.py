"""A tiny TLS IMAP4rev1 server for read-only connector tests.

It implements only what the connector uses and records every command, so tests
can prove the connector never selects read-write or changes flags.
"""

from __future__ import annotations

import datetime
import ipaddress
import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class Mailbox:
    idle_notification: bool = False
    idle_entered: Event = field(default_factory=Event)
    username: str = "owner@example.test"
    password: str = "app-password"
    uidvalidity: int = 7
    messages: dict[int, bytes] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    capabilities: str = "IMAP4rev1 IDLE"

    @property
    def uidnext(self) -> int:
        return max(self.messages, default=100) + 1


def certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "imap-cert.pem", directory / "imap-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert_path, key_path


def _tokens(line: str) -> list[str]:
    parts: list[str] = []
    current, quoted = "", False
    for char in line:
        if char == '"':
            quoted = not quoted
        elif char == " " and not quoted:
            if current:
                parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def _handler(box: Mailbox) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def send(self, data: bytes) -> None:
            self.wfile.write(data)
            self.wfile.flush()

        def handle(self) -> None:
            self.send(b"* OK fake IMAP ready\r\n")
            authenticated = False
            while line := self.rfile.readline():
                text = line.decode().rstrip("\r\n")
                box.commands.append(
                    text if " LOGIN " not in text else text.split(" LOGIN ")[0] + " LOGIN ***"
                )
                tag, *rest = _tokens(text)
                command = rest[0].upper() if rest else ""
                if command == "CAPABILITY":
                    self.send(f"* CAPABILITY {box.capabilities}\r\n{tag} OK done\r\n".encode())
                elif command == "LOGIN":
                    if rest[1:] == [box.username, box.password]:
                        authenticated = True
                        self.send(f"{tag} OK logged in\r\n".encode())
                    else:
                        self.send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials\r\n".encode())
                elif command == "LOGOUT":
                    self.send(f"* BYE bye\r\n{tag} OK done\r\n".encode())
                    return
                elif not authenticated:
                    self.send(f"{tag} BAD not authenticated\r\n".encode())
                elif command == "EXAMINE":
                    self.send(
                        f"* {len(box.messages)} EXISTS\r\n* OK [UIDVALIDITY {box.uidvalidity}] ok\r\n"
                        f"* OK [UIDNEXT {box.uidnext}] ok\r\n{tag} OK [READ-ONLY] done\r\n".encode()
                    )
                elif command == "IDLE":
                    self.send(b"+ idling\r\n")
                    box.idle_entered.set()
                    if box.idle_notification:
                        self.send(f"* {len(box.messages)} EXISTS\r\n".encode())
                    done = self.rfile.readline()
                    if done.strip() == b"DONE":
                        box.commands.append("DONE")
                        self.send(f"{tag} OK idle completed\r\n".encode())
                    else:
                        return
                elif command == "UID" and rest[1].upper() == "SEARCH":
                    start = int(rest[3].split(":")[0])
                    found = [uid for uid in sorted(box.messages) if uid >= start] or sorted(box.messages)[-1:]
                    self.send(f"* SEARCH {' '.join(map(str, found))}\r\n{tag} OK done\r\n".encode())
                elif command == "UID" and rest[1].upper() == "FETCH":
                    uid, item = int(rest[2]), " ".join(rest[3:]).upper()
                    raw = box.messages[uid]
                    if "RFC822.SIZE" in item:
                        self.send(
                            f"* 1 FETCH (UID {uid} RFC822.SIZE {len(raw)})\r\n{tag} OK done\r\n".encode()
                        )
                    else:
                        limit = int(item.rsplit(".", 1)[1].rstrip(">)"))
                        body = raw[:limit]
                        self.send(f"* 1 FETCH (UID {uid} BODY[]<0> {{{len(body)}}}\r\n".encode() + body)
                        self.send(f")\r\n{tag} OK done\r\n".encode())
                else:
                    self.send(f"{tag} BAD unsupported\r\n".encode())

    return Handler


@contextmanager
def serve(box: Mailbox, directory: Path) -> Iterator[tuple[int, ssl.SSLContext]]:
    cert_path, key_path = certificate(directory)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def get_request(self) -> tuple[ssl.SSLSocket, object]:
            sock, address = super().get_request()
            return server_context.wrap_socket(sock, server_side=True), address

    server = Server(("127.0.0.1", 0), _handler(box))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(cert_path))
    try:
        yield server.server_address[1], client_context
    finally:
        server.shutdown()
        server.server_close()


def message(subject: str, sender: str, body: str, *, html: bool = False, message_id: str = "") -> bytes:
    kind = "text/html" if html else "text/plain"
    header = message_id or f"<{abs(hash(subject))}@example.test>"
    return (
        f"From: {sender}\r\nTo: owner@example.test\r\nSubject: {subject}\r\nMessage-ID: {header}\r\n"
        f"Date: Wed, 30 Sep 2026 12:00:00 +0000\r\nContent-Type: {kind}; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()
