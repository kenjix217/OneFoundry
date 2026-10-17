"""Local-only fixture, CONNECT proxy, and hostile client for the Docker egress proof."""

from __future__ import annotations

import json
import os
import select
import socket
import ssl
import sys
from pathlib import Path

PROVIDER = "provider.test"
PROXY_PORT = 3128
FIXTURE_PORT = 443


def _read_headers(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 2048:
        chunk = connection.recv(2048 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _fixture() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/cert.pem", "/tls/key.pem")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", FIXTURE_PORT))  # noqa: S104 - isolated Docker fixture network only
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            try:
                connection.settimeout(3)
                with context.wrap_socket(connection, server_side=True) as secure:
                    request = _read_headers(secure)
                    if request.startswith(b"GET /proof HTTP/1.1\r\nHost: provider.test\r\n"):
                        secure.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nlocal")
                    else:
                        secure.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            except (OSError, ssl.SSLError):
                connection.close()


def _relay(left: socket.socket, right: socket.socket) -> None:
    sides = (left, right)
    while True:
        readable, _, _ = select.select(sides, (), (), 3)
        if not readable:
            return
        for source in readable:
            data = source.recv(8192)
            if not data:
                return
            (right if source is left else left).sendall(data)


def _exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        part = connection.recv(length - len(data))
        if not part:
            raise ValueError("incomplete_tls_record")
        data.extend(part)
    return bytes(data)


def _client_hello(connection: socket.socket) -> bytes:
    """Admit only a bounded, complete TLS ClientHello with the exact test SNI."""
    header = _exact(connection, 5)
    size = int.from_bytes(header[3:5])
    if header[:3] != b"\x16\x03\x01" or not 1 <= size <= 16384:
        raise ValueError("invalid_tls_record")
    body = _exact(connection, size)
    if len(body) < 4 or body[0] != 1 or int.from_bytes(body[1:4]) != len(body) - 4:
        raise ValueError("invalid_client_hello")
    hello = memoryview(body)[4:]
    offset = 34  # legacy version and random
    if len(hello) < offset + 1:
        raise ValueError("short_client_hello")
    session_length = hello[offset]
    offset += 1 + session_length
    if len(hello) < offset + 2:
        raise ValueError("short_ciphers")
    cipher_length = int.from_bytes(hello[offset : offset + 2])
    offset += 2 + cipher_length
    if len(hello) < offset + 1:
        raise ValueError("short_compression")
    compression_length = hello[offset]
    offset += 1 + compression_length
    if len(hello) < offset + 2:
        raise ValueError("missing_extensions")
    extensions_length = int.from_bytes(hello[offset : offset + 2])
    offset += 2
    end = offset + extensions_length
    if end != len(hello):
        raise ValueError("invalid_extensions")
    found: list[bytes] = []
    while offset < end:
        if offset + 4 > end:
            raise ValueError("invalid_extension")
        kind = int.from_bytes(hello[offset : offset + 2])
        length = int.from_bytes(hello[offset + 2 : offset + 4])
        offset += 4
        if offset + length > end:
            raise ValueError("invalid_extension_length")
        if kind == 0:
            names = hello[offset : offset + length]
            if len(names) < 2 or int.from_bytes(names[:2]) != len(names) - 2:
                raise ValueError("invalid_sni_list")
            position = 2
            while position < len(names):
                if position + 3 > len(names):
                    raise ValueError("invalid_sni_entry")
                name_kind = names[position]
                name_length = int.from_bytes(names[position + 1 : position + 3])
                position += 3
                if position + name_length > len(names):
                    raise ValueError("invalid_sni_length")
                if name_kind == 0:
                    found.append(bytes(names[position : position + name_length]))
                position += name_length
        offset += length
    if found != [PROVIDER.encode()]:
        raise ValueError("tls_sni_not_allowed")
    return header + body


def _proxy(fixture_ip: str) -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", PROXY_PORT))  # noqa: S104 - isolated Docker client network only
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    connection.settimeout(3)
                    request = _read_headers(connection)
                    first = request.split(b"\r\n", 1)[0]
                    if first != b"CONNECT provider.test:443 HTTP/1.1":
                        connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                        continue
                    with socket.create_connection((fixture_ip, FIXTURE_PORT), timeout=3) as upstream:
                        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        upstream.sendall(_client_hello(connection))
                        _relay(connection, upstream)
                except (OSError, ValueError):
                    pass


def _connect(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=0.4):
            return True
    except OSError:
        return False


def _request_proxy(ip: str, target: str, *, tls: bool = False) -> bytes:
    with socket.create_connection((ip, PROXY_PORT), timeout=3) as connection:
        connection.settimeout(3)
        connection.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        response = _read_headers(connection)
        if not tls:
            return response
        if not response.startswith(b"HTTP/1.1 200 "):
            return response
        context = ssl.create_default_context(cafile="/tls/cert.pem")
        with context.wrap_socket(connection, server_hostname=PROVIDER) as secure:
            secure.sendall(b"GET /proof HTTP/1.1\r\nHost: provider.test\r\nConnection: close\r\n\r\n")
            return secure.recv(4096)


def _client(proxy_ip: str, fixture_ip: str, host_ip: str, host_port: int) -> None:
    wrong_sni_denied = False
    try:
        with socket.create_connection((proxy_ip, PROXY_PORT), timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(b"CONNECT provider.test:443 HTTP/1.1\r\nHost: provider.test:443\r\n\r\n")
            if _read_headers(connection).startswith(b"HTTP/1.1 200 "):
                context = ssl._create_unverified_context()  # noqa: S323 - negative test only
                with context.wrap_socket(connection, server_hostname="other.test"):
                    pass
    except (OSError, ssl.SSLError):
        wrong_sni_denied = True
    routes = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
    observations = {
        "allowed_tls": b"200 OK" in _request_proxy(proxy_ip, "provider.test:443", tls=True),
        "other_host_denied": b"403 Forbidden" in _request_proxy(proxy_ip, "other.test:443"),
        "other_port_denied": b"403 Forbidden" in _request_proxy(proxy_ip, "provider.test:80"),
        "ip_literal_denied": b"403 Forbidden" in _request_proxy(proxy_ip, f"{fixture_ip}:443"),
        "wrong_sni_denied": wrong_sni_denied,
        "http_form_denied": b"403 Forbidden" in _request_proxy(proxy_ip, "http://provider.test/proof"),
        "fixture_direct_denied": not _connect(fixture_ip, FIXTURE_PORT),
        "host_direct_denied": not _connect(host_ip, host_port),
        "no_default_route": all(line.split()[1] != "00000000" for line in routes),
        "no_host_paths": not any(
            Path(path).exists() for path in ("/model-secrets", "/var/run/docker.sock", "/source-roots")
        )
        and (not Path("/runner-spool").exists() or not any(Path("/runner-spool").iterdir())),
        "no_app_source": not any(Path("/app").iterdir()),
        "no_provider_auth": not any(
            name in os.environ for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_HOME")
        ),
    }
    print(json.dumps(observations, sort_keys=True), flush=True)
    if not all(observations.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["fixture"]:
            _fixture()
        case ["proxy", fixture_ip]:
            _proxy(fixture_ip)
        case ["client", proxy_ip, fixture_ip, host_ip, host_port]:
            _client(proxy_ip, fixture_ip, host_ip, int(host_port))
        case _:
            raise SystemExit("invalid probe role")
