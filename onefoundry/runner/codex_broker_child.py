"""Offline TLS-terminating broker with a dummy secret outside the Codex child.

Only a local fixture is reachable upstream. This proof buffers bounded SSE
responses to inspect reported usage; it is not a live billing control.
"""

from __future__ import annotations

import json
import socket
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner.codex_broker_policy import (
    LIMITS,
    BrokerDenied,
    admit_connect,
    admit_request,
    admit_request_number,
    admit_total_tokens,
    response_tokens,
)


@dataclass
class _Observed:
    requests: int = 0
    total_tokens: int = 0


def _record(kind: str, **fields: object) -> None:
    print(json.dumps({"type": kind, **fields}, sort_keys=True), flush=True)


def _read_message(
    connection: socket.socket | ssl.SSLSocket, body_limit: int
) -> tuple[str, dict[str, str], bytes]:
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        if len(raw) >= 8192:
            raise BrokerDenied("header_limit")
        part = connection.recv(min(4096, 8192 - len(raw)))
        if not part:
            raise BrokerDenied("incomplete_headers")
        raw.extend(part)
    head, _, body = raw.partition(b"\r\n\r\n")
    try:
        lines = head.decode("ascii").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator != ":" or name.lower() in headers:
                raise BrokerDenied("header_invalid")
            headers[name.lower()] = value.strip()
        length = int(headers["content-length"])
    except (UnicodeDecodeError, KeyError, ValueError) as exc:
        raise BrokerDenied("header_invalid") from exc
    if length < 0 or length > body_limit or len(body) > length:
        raise BrokerDenied("body_limit")
    while len(body) < length:
        part = connection.recv(min(8192, length - len(body)))
        if not part:
            raise BrokerDenied("incomplete_body")
        body += part
    return lines[0], headers, bytes(body)


def _read_connect(connection: socket.socket) -> bytes:
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        if len(raw) >= 2048:
            raise BrokerDenied("connect_header_limit")
        part = connection.recv(2048 - len(raw))
        if not part:
            raise BrokerDenied("incomplete_connect")
        raw.extend(part)
    head, _, remainder = raw.partition(b"\r\n\r\n")
    if remainder:
        raise BrokerDenied("connect_extra_data")
    return bytes(head.split(b"\r\n", 1)[0])


def _server_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain("/tls/leaf-cert.pem", "/tls/leaf-key.pem")

    def exact_sni(_connection: ssl.SSLSocket | ssl.SSLObject, name: str | None, _context: Any) -> int | None:
        return None if name == "provider.test" else ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME

    context.set_servername_callback(exact_sni)
    return context


def _upstream(fixture_ip: str, secret: bytes, body: bytes) -> bytes:
    context = ssl.create_default_context(cafile="/tls/cert.pem")
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((fixture_ip, 443), timeout=4) as connection:
        with context.wrap_socket(connection, server_hostname="provider.test") as secure:
            secure.settimeout(4)
            secure.sendall(
                b"POST /v1/responses HTTP/1.1\r\n"
                b"Host: provider.test\r\n"
                b"Content-Type: application/json\r\n"
                b"Accept: text/event-stream\r\n"
                b"Authorization: Bearer "
                + secret
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            line, headers, response = _read_message(secure, LIMITS.max_response_bytes)
    if line != "HTTP/1.1 200 OK" or headers.get("content-type") != "text/event-stream":
        raise BrokerDenied("upstream_response_invalid")
    return response


def _serve_one(
    connection: socket.socket,
    fixture_ip: str,
    secret: bytes,
    context: ssl.SSLContext,
    observed: _Observed,
) -> None:
    connection.settimeout(4)
    try:
        admit_connect(_read_connect(connection))
    except BrokerDenied as exc:
        _record("broker.denied", reason=exc.code)
        connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return
    connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    with context.wrap_socket(connection, server_side=True) as secure:
        secure.settimeout(4)
        try:
            line, headers, body = _read_message(secure, LIMITS.max_request_bytes)
            admit_request_number(observed.requests)
            bounded = admit_request(line, headers, body)
            # Count a send before contacting upstream. A failed send cannot be retried.
            observed.requests += 1
            response = _upstream(fixture_ip, secret, bounded)
            tokens = response_tokens(response)
            admit_total_tokens(observed.total_tokens, tokens)
            observed.total_tokens += tokens
            secure.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + b"Content-Length: "
                + str(len(response)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + response
            )
            _record(
                "broker.forwarded",
                path="/v1/responses",
                request_bytes=len(bounded),
                output_token_ceiling=LIMITS.max_output_tokens_per_request,
                observed_tokens=tokens,
                sequence=observed.requests,
            )
        except BrokerDenied as exc:
            _record("broker.denied", reason=exc.code)
            secure.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")


def main(fixture_ip: str) -> None:
    secret = Path("/broker-secret").read_bytes()
    if secret != b"local-test-only":
        raise BrokerDenied("fixture_secret_invalid")
    context = _server_context()
    observed = _Observed()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 3128))  # noqa: S104 - isolated Docker client network
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    _serve_one(connection, fixture_ip, secret, context, observed)
                except (OSError, ssl.SSLError, BrokerDenied):
                    _record("broker.denied", reason="transport_or_tls_invalid")


if __name__ == "__main__":
    main(sys.argv[1])
