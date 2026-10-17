"""Gateway-owned bounded HTTP and Streamable HTTP MCP transport.

Exact approved origins, pinned DNS, no redirects/proxies/cookies, no retries.
Transport errors contain codes only, never endpoints, payloads or credentials.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from importlib.metadata import version
from time import monotonic
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from mcp.types import LATEST_PROTOCOL_VERSION


class TransportFailure(Exception):
    """A bounded transport rejected a request or observed a dependency failure."""


def canonical_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise TransportFailure("invalid_endpoint") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
        or len(value.encode()) > 2048
    ):
        raise TransportFailure("invalid_endpoint")
    host = parsed.hostname.lower()
    display = f"[{host}]" if ":" in host else host
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{display}" + (f":{port}" if port and port != default else "")


def validate_endpoint(value: str, approved: tuple[str, ...]) -> str:
    origin = canonical_origin(value)
    if origin not in approved:
        raise TransportFailure("destination_not_approved")
    path = unquote(urlsplit(value).path)
    if ".." in path.split("/") or "//" in path or "\\" in path:
        raise TransportFailure("invalid_endpoint")
    return origin


def _address(host: str, port: int) -> str:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise TransportFailure("resolution_failed") from None
    addresses = {str(answer[4][0]) for answer in answers}
    if not addresses:
        raise TransportFailure("resolution_failed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # Only an explicitly configured literal or localhost can opt into local
        # services. A public hostname resolving into a private range is rejected.
        if (
            ip.is_link_local
            or ip.is_unspecified
            or ip.is_multicast
            or (not ip.is_global and not (literal == ip or host == "localhost" and ip.is_loopback))
        ):
            raise TransportFailure("destination_address_rejected")
    return sorted(addresses)[0]


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float, tls: bool) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self.address, self.tls = address, tls

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(self.address):
                raise TransportFailure("destination_address_changed")
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


class ExtensionTransport:
    def __init__(self, allowed_origins: tuple[str, ...], *, timeout: float, max_bytes: int) -> None:
        if timeout <= 0 or max_bytes < 1:
            raise ValueError("transport bounds must be positive")
        self.allowed_origins = tuple(canonical_origin(origin) for origin in allowed_origins)
        self.timeout, self.max_bytes = timeout, max_bytes

    def request(
        self,
        endpoint: str,
        method: str,
        path: str,
        payload: bytes | None,
        headers: dict[str, str],
        *,
        deadline: float | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        validate_endpoint(endpoint, self.allowed_origins)
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        assert host is not None
        decoded = unquote(path)
        if not path.startswith("/") or path.startswith("//") or ".." in decoded.split("/") or "\\" in decoded:
            raise TransportFailure("invalid_operation_path")
        if any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise TransportFailure("invalid_operation_path")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        deadline = deadline or monotonic() + self.timeout
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TransportFailure("request_timeout")
        address = _address(host, port)
        if parsed.scheme == "http" and not ipaddress.ip_address(address).is_loopback:
            raise TransportFailure("plaintext_destination_rejected")
        connection = _PinnedConnection(host, port, address, remaining, parsed.scheme == "https")
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise TransportFailure("redirect_rejected")
            if not 200 <= response.status < 300:
                raise TransportFailure("provider_rejected")
            chunks: list[bytes] = []
            size = 0
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TransportFailure("request_timeout")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(65536, self.max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_bytes:
                    raise TransportFailure("response_too_large")
                chunks.append(chunk)
            return b"".join(chunks), {k.lower(): v for k, v in response.getheaders()}
        except TransportFailure:
            raise
        except TimeoutError:
            raise TransportFailure("request_timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise TransportFailure("connection_failed") from None
        finally:
            connection.close()

    def http(
        self,
        endpoint: str,
        method: str,
        path: str,
        arguments: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise TransportFailure("invalid_method")
        remaining_arguments = dict(arguments)

        def replace_parameter(match: re.Match[str]) -> str:
            name = match.group(1)
            value = remaining_arguments.pop(name, None)
            if type(value) not in {str, int} or not str(value) or str(value) in {".", ".."}:
                raise TransportFailure("invalid_path_parameter")
            return quote(str(value), safe="")

        resolved = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace_parameter, path)
        if "{" in resolved or "}" in resolved:
            raise TransportFailure("invalid_path_template")
        target = urlsplit(endpoint).path.rstrip("/") + resolved
        outgoing = {"Accept": "application/json", **(headers or {})}
        payload = None
        if method == "GET":
            query = urlencode(
                {
                    key: value if isinstance(value, str) else json.dumps(value)
                    for key, value in remaining_arguments.items()
                }
            )
            target += ("&" if "?" in target else "?") + query if query else ""
        else:
            payload = json.dumps(
                remaining_arguments, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode()
            outgoing["Content-Type"] = "application/json"
        body, _ = self.request(endpoint, method, target, payload, outgoing)
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise TransportFailure("invalid_response") from None
        if not isinstance(result, dict):
            raise TransportFailure("invalid_response")
        return result

    def _rpc(
        self,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        deadline: float,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        payload, returned = self.request(
            endpoint,
            "POST",
            urlsplit(endpoint).path or "/",
            json.dumps(body, allow_nan=False, separators=(",", ":")).encode(),
            {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers},
            deadline=deadline,
        )
        if "id" not in body:
            return {}, returned
        try:
            if "text/event-stream" in returned.get("content-type", ""):
                candidates = [
                    json.loads(line[5:].strip())
                    for line in payload.decode().splitlines()
                    if line.startswith("data:")
                ]
            else:
                candidates = [json.loads(payload)]
            matches = [item for item in candidates if isinstance(item, dict) and item.get("id") == body["id"]]
            if len(matches) != 1 or "error" in matches[0] or matches[0].get("jsonrpc") != "2.0":
                raise ValueError
            result = matches[0]["result"]
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, KeyError, UnicodeError):
            raise TransportFailure("invalid_mcp_response") from None
        return result, returned

    def _session(self, endpoint: str, headers: dict[str, str], deadline: float) -> dict[str, str]:
        result, response = self._rpc(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "onefoundry", "version": version("onefoundry")},
                },
            },
            headers,
            deadline,
        )
        protocol = result.get("protocolVersion")
        if protocol != LATEST_PROTOCOL_VERSION:
            raise TransportFailure("mcp_protocol_unavailable")
        headers = {**headers, "MCP-Protocol-Version": protocol}
        session = response.get("mcp-session-id")
        if session:
            if len(session) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in session):
                raise TransportFailure("invalid_mcp_session")
            headers["Mcp-Session-Id"] = session
        self._rpc(endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers, deadline)
        return headers

    def discover(self, endpoint: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
        deadline = monotonic() + self.timeout
        actual = self._session(endpoint, headers or {}, deadline)
        result, _ = self._rpc(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, actual, deadline)
        tools = result.get("tools")
        if result.get("nextCursor") or not isinstance(tools, list) or len(tools) > 100:
            raise TransportFailure("tool_contract_changed")
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in tools):
            raise TransportFailure("tool_contract_changed")
        return tools

    def mcp(
        self,
        endpoint: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        expected_schema: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        deadline = monotonic() + self.timeout
        actual = self._session(endpoint, headers or {}, deadline)
        listed, _ = self._rpc(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, actual, deadline)
        tools = listed.get("tools", [])
        if not isinstance(tools, list) or listed.get("nextCursor"):
            raise TransportFailure("tool_contract_changed")
        matched = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == operation]
        if len(matched) != 1 or matched[0].get("inputSchema") != expected_schema:
            raise TransportFailure("tool_contract_changed")
        result, _ = self._rpc(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": operation, "arguments": arguments},
            },
            actual,
            deadline,
        )
        if result.get("isError"):
            raise TransportFailure("provider_rejected")
        value = result.get("structuredContent")
        if value is None:
            blocks = result.get("content", [])
            texts = [
                block.get("text")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            try:
                value = json.loads(texts[0]) if len(texts) == 1 and isinstance(texts[0], str) else None
            except (ValueError, TypeError):
                value = None
        if not isinstance(value, dict):
            raise TransportFailure("invalid_mcp_response")
        return value
