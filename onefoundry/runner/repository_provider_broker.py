"""One-job HTTP broker; only its configured HTTPS provider receives credentials."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from runner.repository_provider_policy import ProviderBounds, admit_request

REJECTION_CODES = frozenset(
    {
        "server_is_overloaded",
        "server_error",
        "rate_limit_exceeded",
        "usage_limit_reached",
        "context_length_exceeded",
        "context_window_exceeded",
        "invalid_prompt",
        "model_not_found",
        "insufficient_quota",
    }
)


def validate_response_events(result: bytes) -> None:
    """Validate SSE records without mistaking the optional end marker for JSON."""
    terminal: list[str] = []
    rejections: list[str] = []
    data: list[bytes] = []
    ended = False
    for line in [*result.splitlines(), b""]:
        if line:
            field, _, value = line.partition(b":")
            if field == b"data":
                data.append(value[1:] if value.startswith(b" ") else value)
            continue
        if not data:
            continue
        payload = b"\n".join(data)
        data.clear()
        if ended:
            raise ValueError("repository_provider_response_invalid")
        if payload == b"[DONE]":
            ended = True
            continue
        try:
            item = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("repository_provider_response_invalid") from exc
        if not isinstance(item, dict):
            raise ValueError("repository_provider_response_invalid")
        kind = item.get("type")
        if not isinstance(kind, str):
            raise ValueError("repository_provider_response_invalid")
        if kind in {"error", "response.failed"}:
            response = item.get("response", {})
            error = response.get("error", {}) if isinstance(response, dict) else {}
            code = (
                item.get("code")
                if kind == "error"
                else error.get("code")
                if isinstance(error, dict)
                else None
            )
            rejections.append(code if isinstance(code, str) and code in REJECTION_CODES else "unknown")
        elif kind in {"response.completed", "response.incomplete"}:
            terminal.append(kind)
    if not terminal and len(rejections) == 1:
        raise ValueError("repository_provider_rejected_" + rejections[0])
    if terminal == ["response.incomplete"] and not rejections:
        raise ValueError("repository_provider_incomplete")
    if not terminal and not rejections:
        raise ValueError("repository_provider_terminal_missing")
    if terminal != ["response.completed"] or rejections:
        raise ValueError("repository_provider_terminal_unproved")


def failure_reason(error: Exception) -> str:
    """Keep transport identity while excluding arbitrary upstream exception text."""
    if isinstance(error, TimeoutError):
        return "repository_transport_timeout"
    if isinstance(error, socket.gaierror):
        return "repository_transport_dns_failed"
    if isinstance(error, ssl.SSLError):
        return "repository_transport_tls_failed"
    if isinstance(error, http.client.IncompleteRead):
        return "repository_transport_incomplete_body"
    if isinstance(error, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
        return "repository_transport_connection_closed"
    if isinstance(error, http.client.HTTPException):
        return "repository_transport_protocol_failed"
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return "repository_provider_response_invalid"
    if type(error) is ValueError and str(error).startswith("repository_"):
        return str(error)
    return "provider_transport_unavailable"


def forward(config: dict[str, Any], token: bytes, body: bytes) -> bytes:
    parsed = urlsplit(config["upstream_url"])
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("repository_provider_origin_invalid")
    fixture = config.get("fixture_upstream_ip")
    ca_file = config.get("fixture_ca_file")
    if fixture is not None:
        if host != "provider.test" or ca_file != "/fixture-tls/cert.pem":
            raise ValueError("repository_fixture_route_invalid")
        address = str(ipaddress.ip_address(fixture))
    else:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        public = [str(entry[4][0]) for entry in addresses if ipaddress.ip_address(entry[4][0]).is_global]
        if not public or len(public) != len(addresses) or ca_file is not None:
            raise ValueError("repository_provider_address_denied")
        address = public[0]
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    account_header = b""
    if config.get("auth_kind", "api_key") == "codex_subscription":
        account = config.get("account_id")
        if (
            config["upstream_url"] != "https://chatgpt.com/backend-api/codex/responses"
            or not isinstance(account, str)
            or not account
            or any(ord(char) < 33 or ord(char) > 126 for char in account)
        ):
            raise ValueError("repository_subscription_route_invalid")
        account_header = (
            b"\r\nchatgpt-account-id: "
            + account.encode("ascii")
            + b"\r\nopenai-beta: responses=experimental\r\noriginator: onefoundry"
        )
    with socket.create_connection(
        (address, parsed.port or 443), timeout=config["request_timeout_seconds"]
    ) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secure:
            secure.settimeout(config["request_timeout_seconds"])
            secure.sendall(
                b"POST "
                + parsed.path.encode("ascii")
                + b" HTTP/1.1\r\nHost: "
                + parsed.netloc.encode("ascii")
                + b"\r\nAuthorization: Bearer "
                + token
                + account_header
                + b"\r\nContent-Type: application/json\r\n"
                + b"Accept: text/event-stream\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            response = http.client.HTTPResponse(secure)
            response.begin()
            if response.status != 200:
                raise ValueError(f"repository_provider_http_{response.status}")
            media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            # The Codex subscription endpoint can omit Content-Type on a valid
            # SSE response. Its bounded body still needs the terminal proof below.
            if media_type != "text/event-stream" and not (
                media_type == "" and config.get("auth_kind") == "codex_subscription"
            ):
                raise ValueError("repository_provider_response_invalid")
            result = response.read(config["max_response_bytes"] + 1)
    # Buffering keeps partial secret echoes and incomplete Responses streams out
    # of the coding child. Lost external effects remain unknown; there is no retry.
    if len(result) > config["max_response_bytes"] or token in result:
        raise ValueError("repository_provider_response_denied")
    validate_response_events(result)
    return result


@dataclass
class BrokerRequests:
    """Account for every upstream request, including known rejected generations."""

    config: dict[str, Any]
    token: bytes
    bounds: ProviderBounds
    requests: int = 0

    def send(self, raw: bytes) -> bytes:
        retries = int(self.config.get("provider_overload_retries", 0))
        for retry in range(retries + 1):
            body = admit_request(raw, self.bounds, self.requests)
            self.requests += 1
            try:
                result = forward(self.config, self.token, body)
            except ValueError as exc:
                if (
                    type(exc) is not ValueError
                    or str(exc) != "repository_provider_rejected_server_is_overloaded"
                    or retry == retries
                    or self.requests >= self.bounds.max_requests
                ):
                    raise
                # No response has reached the child: only this exact rejected
                # generation can retry. Network loss and unknown events cannot.
                print(
                    json.dumps({"type": "repository_broker.retrying", "request_number": self.requests}),
                    flush=True,
                )
                time.sleep(self.config["provider_overload_retry_delay_seconds"])
                continue
            print(
                json.dumps(
                    {
                        "type": "repository_broker.forwarded",
                        "request_number": self.requests,
                        "request_bytes": len(body),
                        "response_bytes": len(result),
                    }
                ),
                flush=True,
            )
            return result
        raise RuntimeError("repository_provider_retry_state_invalid")


def serve(config: dict[str, Any], token: bytes) -> None:
    bounds = ProviderBounds(
        **{name: config[name] for name in ProviderBounds.__dataclass_fields__ if name in config}
    )
    requests = BrokerRequests(config, token, bounds)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: Any) -> None:
            pass  # Paths, requests and provider error bodies never enter logs.

        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP method contract
            try:
                if (
                    self.path != "/v1/responses"
                    or self.headers.get("Authorization") != "Bearer client-placeholder-only"
                ):
                    raise ValueError("repository_broker_route_denied")
                if self.client_address[0] != config["client_ip"]:
                    raise ValueError("repository_broker_client_denied")
                if (
                    self.headers.get("Transfer-Encoding")
                    or len(self.headers.get_all("Content-Length", [])) != 1
                ):
                    raise ValueError("repository_broker_framing_invalid")
                length = int(self.headers["Content-Length"])
                if not 0 < length <= bounds.max_request_bytes:
                    raise ValueError("repository_broker_body_limit")
                self.connection.settimeout(config["request_timeout_seconds"])
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("repository_broker_body_incomplete")
                if config.get("fixture_upstream_ip") is not None:
                    tools = json.loads(raw).get("tools", [])
                    print(
                        json.dumps(
                            {
                                "type": "fixture.tool_inventory",
                                "tools": [
                                    {
                                        "type": item.get("type"),
                                        "name": item.get("name"),
                                        "children": [
                                            {"type": child.get("type"), "name": child.get("name")}
                                            for child in item.get("tools", [])
                                        ],
                                    }
                                    for item in tools
                                ],
                            }
                        ),
                        flush=True,
                    )
                result = requests.send(raw)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                reason = failure_reason(exc)
                print(json.dumps({"type": "repository_broker.denied", "reason": reason}), flush=True)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

    with HTTPServer(("0.0.0.0", config["broker_port"]), Handler) as server:  # noqa: S104 - isolated client network
        print("READY", flush=True)
        server.serve_forever()


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text())
    token = Path("/provider-credential").read_bytes().strip()
    if not token or len(token) > 8192 or any(char < 33 or char > 126 for char in token):
        raise ValueError("repository_provider_credential_invalid")
    serve(config, token)


if __name__ == "__main__":
    main()
