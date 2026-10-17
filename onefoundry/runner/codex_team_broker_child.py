"""Isolated, single-turn TLS broker for a scoped Codex app-server session.

The supervisor supplies one OAuth access token through stdin. It never enters
Codex's environment, mounts, rollout, command line, or Docker metadata. This
process accepts only the configured provider's Responses path and OneFoundry
tools, then uses verified TLS to reach that exact upstream.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
from dataclasses import dataclass
from typing import Any

from runner.codex_team_broker_policy import (
    TeamRequestDenied,
    TeamRequestLimits,
    admit_request_number,
    admit_team_request,
)


class BrokerProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    request_path: str
    upstream: str
    limits: TeamRequestLimits
    max_response_bytes: int
    upstream_ca_file: str | None

    def __post_init__(self) -> None:
        if self.host not in {"api.openai.com", "chatgpt.com", "provider.test"}:
            raise ValueError("invalid team provider host")
        if self.request_path not in {"/v1/responses", "/backend-api/codex/responses"}:
            raise ValueError("invalid team provider path")
        if self.host == "api.openai.com" and self.request_path != "/v1/responses":
            raise ValueError("invalid ChatGPT plan route")
        if self.host == "chatgpt.com" and self.request_path != "/backend-api/codex/responses":
            raise ValueError("invalid existing account route")
        if self.host in {"api.openai.com", "chatgpt.com"}:
            if self.upstream != self.host or self.upstream_ca_file is not None:
                raise ValueError("live provider route is invalid")
        else:
            try:
                ipaddress.ip_address(self.upstream)
            except ValueError as exc:
                raise ValueError("fixture provider address is invalid") from exc
            if self.upstream_ca_file != "/tls/cert.pem":
                raise ValueError("fixture provider trust is invalid")
        if not 1 <= self.max_response_bytes <= 16_000_000:
            raise ValueError("invalid provider response bound")


def _read_headers(connection: socket.socket | ssl.SSLSocket, limit: int) -> tuple[str, dict[str, str], bytes]:
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        if len(raw) >= limit:
            raise BrokerProtocolError("header_limit")
        chunk = connection.recv(min(4096, limit - len(raw)))
        if not chunk:
            raise BrokerProtocolError("incomplete_header")
        raw.extend(chunk)
    head, _, body = raw.partition(b"\r\n\r\n")
    try:
        lines = head.decode("ascii").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            key = name.lower()
            if separator != ":" or not key or key in headers:
                raise BrokerProtocolError("invalid_header")
            headers[key] = value.strip()
    except UnicodeDecodeError as exc:
        raise BrokerProtocolError("invalid_header") from exc
    return lines[0], headers, bytes(body)


def _read_connect(connection: socket.socket, host: str) -> None:
    line, _headers, remainder = _read_headers(connection, 2048)
    if line != f"CONNECT {host}:443 HTTP/1.1" or remainder:
        raise BrokerProtocolError("connect_target_denied")


def _read_request(connection: ssl.SSLSocket, config: BrokerConfig) -> bytes:
    line, headers, body = _read_headers(connection, 8192)
    if line != f"POST {config.request_path} HTTP/1.1":
        raise BrokerProtocolError("request_path_denied")
    if headers.get("host") not in {config.host, f"{config.host}:443"}:
        raise BrokerProtocolError("request_host_denied")
    if headers.get("authorization") != "Bearer client-placeholder-only":
        raise BrokerProtocolError("client_placeholder_invalid")
    if headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise BrokerProtocolError("request_content_type_invalid")
    if "transfer-encoding" in headers:
        raise BrokerProtocolError("request_framing_invalid")
    try:
        length = int(headers["content-length"])
    except (KeyError, ValueError) as exc:
        raise BrokerProtocolError("request_framing_invalid") from exc
    if length < len(body) or length > config.limits.max_request_bytes:
        raise BrokerProtocolError("request_size_limit")
    while len(body) < length:
        chunk = connection.recv(min(8192, length - len(body)))
        if not chunk:
            raise BrokerProtocolError("request_body_incomplete")
        body += chunk
    return body


def _server_context(host: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain("/tls/leaf-cert.pem", "/tls/leaf-key.pem")

    def check_sni(_socket: ssl.SSLSocket | ssl.SSLObject, name: str | None, _context: Any) -> int | None:
        return None if name == host else ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME

    context.set_servername_callback(check_sni)
    return context


def _provider_request(
    config: BrokerConfig, token: bytes, account_id: bytes | None, body: bytes, downstream: ssl.SSLSocket
) -> tuple[int, int]:
    context = ssl.create_default_context(cafile=config.upstream_ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((config.upstream, 443), timeout=30) as raw:
        with context.wrap_socket(raw, server_hostname=config.host) as secure:
            secure.settimeout(120)
            account_headers = (
                b"chatgpt-account-id: "
                + account_id
                + b"\r\nopenai-beta: responses=experimental\r\noriginator: onefoundry\r\n"
                if account_id is not None
                else b""
            )
            secure.sendall(
                b"POST "
                + config.request_path.encode("ascii")
                + b" HTTP/1.1\r\nHost: "
                + config.host.encode("ascii")
                + b"\r\nAuthorization: Bearer "
                + token
                + b"\r\n"
                + account_headers
                + b"Content-Type: application/json\r\nAccept: text/event-stream\r\n"
                + b"Connection: close\r\nContent-Length: "
                + str(len(body)).encode("ascii")
                + b"\r\n\r\n"
                + body
            )
            response = http.client.HTTPResponse(secure)
            response.begin()
            status = response.status
            if status != 200:
                _send_response(downstream, status, b"")
                return status, 0
            content_type = response.getheader("content-type", "").split(";", 1)[0].lower()
            if content_type != "text/event-stream":
                raise BrokerProtocolError("provider_content_type_invalid")
            downstream.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
            )
            total = 0
            while chunk := response.read1(8192):
                total += len(chunk)
                if total > config.max_response_bytes:
                    raise BrokerProtocolError("provider_response_size_limit")
                downstream.sendall(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            downstream.sendall(b"0\r\n\r\n")
            return status, total


def _send_response(connection: ssl.SSLSocket, status: int, body: bytes) -> None:
    if status == 200:
        first = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
    else:
        first = f"HTTP/1.1 {status} Provider Error\r\n".encode("ascii")
    connection.sendall(
        first + b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\nConnection: close\r\n\r\n" + body
    )


def _serve_one(
    connection: socket.socket,
    config: BrokerConfig,
    token: bytes,
    account_id: bytes | None,
    context: ssl.SSLContext,
    previous_requests: int,
) -> bool:
    connection.settimeout(30)
    try:
        _read_connect(connection, config.host)
    except BrokerProtocolError as exc:
        print(json.dumps({"type": "team_broker.denied", "reason": exc.code}), flush=True)
        connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        return False
    connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    with context.wrap_socket(connection, server_side=True) as secure:
        secure.settimeout(30)
        try:
            admit_request_number(previous_requests, config.limits)
            body = _read_request(secure, config)
            admitted = admit_team_request(body, config.limits)
            incoming = json.loads(body)
            native_prompt_removed = any(
                isinstance(tool, dict) and tool.get("name") == "request_user_input"
                for tool in incoming["tools"]
            )
        except (BrokerProtocolError, TeamRequestDenied) as exc:
            print(json.dumps({"type": "team_broker.denied", "reason": exc.code}), flush=True)
            _send_response(secure, 403, b"")
            return False
        # Count before the external send. A lost reply is not a safe retry.
        try:
            status, response_bytes = _provider_request(config, token, account_id, admitted, secure)
        except (BrokerProtocolError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
            print(
                json.dumps(
                    {
                        "type": "team_broker.outcome_unknown",
                        "error_kind": type(exc).__name__,
                        "reason": exc.code if isinstance(exc, BrokerProtocolError) else None,
                    }
                ),
                flush=True,
            )
            # A provider may already have sent chunks. Closing an incomplete
            # stream preserves the unknown outcome instead of forging success.
            return True
        print(
            json.dumps(
                {
                    "type": "team_broker.forwarded",
                    "status": status,
                    "request_bytes": len(admitted),
                    "response_bytes": response_bytes,
                    "native_prompt_removed": native_prompt_removed,
                }
            ),
            flush=True,
        )
        return True


def _credential_from_stdin(request_path: str) -> tuple[bytes, bytes | None]:
    raw = sys.stdin.buffer.readline(8193)
    token = raw[:-1] if raw.endswith(b"\n") else b""
    if not token or len(token) > 8192 or any(char < 33 or char > 126 for char in token):
        raise ValueError("broker credential unavailable")
    if request_path == "/v1/responses":
        return token, None
    account_id = sys.stdin.buffer.readline(201)
    if not account_id.endswith(b"\n") or not re.fullmatch(rb"[A-Za-z0-9._-]{1,199}", account_id[:-1]):
        raise ValueError("broker account binding unavailable")
    return token, account_id[:-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--request-path", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-request-bytes", type=int, required=True)
    parser.add_argument("--max-requests", type=int, required=True)
    parser.add_argument("--max-response-bytes", type=int, required=True)
    parser.add_argument("--upstream-ca-file")
    args = parser.parse_args()
    config = BrokerConfig(
        args.host,
        args.request_path,
        args.upstream,
        TeamRequestLimits(args.model, args.max_request_bytes, args.max_requests),
        args.max_response_bytes,
        args.upstream_ca_file,
    )
    token, account_id = _credential_from_stdin(config.request_path)
    context = _server_context(config.host)
    observed_requests = 0
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 3128))  # noqa: S104 - isolated client network
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    observed_requests += int(
                        _serve_one(connection, config, token, account_id, context, observed_requests)
                    )
                except (OSError, ssl.SSLError):
                    print(json.dumps({"type": "team_broker.transport_denied"}), flush=True)


if __name__ == "__main__":
    main()
