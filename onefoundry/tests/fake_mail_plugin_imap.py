"""Synthetic TLS IMAP server with exact UID windows, failures, and size probes."""

from __future__ import annotations

import re
import shlex
import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes
from pathlib import Path

from tests.fake_imap import Mailbox, certificate


@dataclass
class PluginMailbox(Mailbox):
    search_override: bytes | None = None
    size_uid_override: int | None = None
    literal_size: int | None = None
    fail_fetch: bool = False
    invalid_identity: bool = False


def _handler(box: PluginMailbox) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def send(self, data: bytes) -> None:
            self.wfile.write(data)
            self.wfile.flush()

        def handle(self) -> None:
            try:
                self.run_commands()
            except (OSError, ValueError):
                return

        def run_commands(self) -> None:
            self.send(b"* OK synthetic IMAP ready\r\n")
            authenticated = False
            while line := self.rfile.readline():
                text = line.decode().rstrip("\r\n")
                box.commands.append(
                    text if " LOGIN " not in text else text.split(" LOGIN ")[0] + " LOGIN ***"
                )
                tag, *rest = shlex.split(text)
                command = rest[0].upper()
                if command == "CAPABILITY":
                    self.send(f"* CAPABILITY {box.capabilities}\r\n{tag} OK done\r\n".encode())
                elif command == "LOGIN":
                    authenticated = rest[1:] == [box.username, box.password]
                    self.send(f"{tag} {'OK' if authenticated else 'NO'} login\r\n".encode())
                elif command == "LOGOUT":
                    self.send(f"* BYE bye\r\n{tag} OK done\r\n".encode())
                    return
                elif not authenticated:
                    self.send(f"{tag} BAD authenticate\r\n".encode())
                elif command == "EXAMINE":
                    validity = "oops" if box.invalid_identity else str(box.uidvalidity)
                    self.send(
                        f"* {len(box.messages)} EXISTS\r\n* OK [UIDVALIDITY {validity}] ok\r\n"
                        f"* OK [UIDNEXT {box.uidnext}] ok\r\n{tag} OK [READ-ONLY] done\r\n".encode()
                    )
                elif rest[:3] == ["UID", "SEARCH", "UID"]:
                    lower, upper = map(int, rest[3].split(":"))
                    found: list[int] = []
                    for uid, raw in sorted(box.messages.items()):
                        if not lower <= uid <= upper:
                            continue
                        parsed = message_from_bytes(raw)
                        filters = list(zip(rest[4::2], rest[5::2], strict=True))
                        matched = True
                        for key, value in filters:
                            source = str(parsed.get("From" if key == "FROM" else "Subject", ""))
                            if key == "TEXT":
                                source = raw.decode()
                            if key in {"TEXT", "FROM", "SUBJECT"} and value.lower() not in source.lower():
                                matched = False
                        if matched:
                            found.append(uid)
                    response = (
                        box.search_override
                        if box.search_override is not None
                        else b" ".join(str(uid).encode() for uid in found)
                    )
                    self.send(b"* SEARCH " + response + f"\r\n{tag} OK done\r\n".encode())
                elif command == "UID" and rest[1].upper() == "FETCH":
                    if box.fail_fetch:
                        self.send(f"{tag} NO synthetic failure\r\n".encode())
                        continue
                    uid, item = int(rest[2]), " ".join(rest[3:]).upper()
                    if uid not in box.messages:
                        self.send(f"{tag} OK absent\r\n".encode())
                        continue
                    raw = box.messages[uid]
                    response_uid = box.size_uid_override or uid
                    if "RFC822.SIZE" in item:
                        self.send(
                            (
                                f"* 1 FETCH (UID {response_uid} RFC822.SIZE {len(raw)})\r\n{tag} OK done\r\n"
                            ).encode()
                        )
                    else:
                        match = re.search(r"<0\.(\d+)>", item)
                        assert match is not None
                        section = (
                            "HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)"
                            if "HEADER.FIELDS" in item
                            else ""
                        )
                        body = (raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n") if section else raw
                        body = body[: int(match[1])]
                        literal_size = box.literal_size if box.literal_size is not None else len(body)
                        self.send(f"* 1 FETCH (UID {uid} BODY[{section}]<0> {{{literal_size}}}\r\n".encode())
                        if box.literal_size is None:
                            self.send(body + f")\r\n{tag} OK done\r\n".encode())
                else:
                    self.send(f"{tag} BAD unsupported\r\n".encode())

    return Handler


@contextmanager
def serve(box: PluginMailbox, directory: Path) -> Iterator[tuple[int, ssl.SSLContext]]:
    cert, key = certificate(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def get_request(self) -> tuple[ssl.SSLSocket, object]:
            sock, address = super().get_request()
            return context.wrap_socket(sock, server_side=True), address

    server = Server(("127.0.0.1", 0), _handler(box))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], ssl.create_default_context(cafile=str(cert))
    finally:
        server.shutdown()
        server.server_close()
