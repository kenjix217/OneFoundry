"""Real local transport tests for exact approved extension destinations."""

import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from onefoundry.gateway_extension_transport import ExtensionTransport, TransportFailure


@pytest.fixture
def service() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"company": "Example", "path": self.path}).encode())

        def do_POST(self) -> None:
            body: dict[str, Any] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = body.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result: object = {
                    "protocolVersion": body["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                result = {"structuredContent": {"company": "Example"}, "content": [], "isError": False}
            else:
                result = body
            payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_exact_approved_api_service(service: str) -> None:
    client = ExtensionTransport((service,), timeout=2, max_bytes=4096)
    result = client.http(service, "GET", "/lookup", {"q": "Example & Sons"})
    assert result == {"company": "Example", "path": "/lookup?q=Example+%26+Sons"}


def test_api_path_parameters_are_encoded_and_removed_from_query(service: str) -> None:
    client = ExtensionTransport((service,), timeout=2, max_bytes=4096)
    result = client.http(service, "GET", "/records/{id}", {"id": "Example & Sons", "q": "active"})
    assert result["path"] == "/records/Example%20%26%20Sons?q=active"
    examples: list[dict[str, Any]] = [{}, {"id": ".."}, {"id": {}}, {"id": True}]
    for arguments in examples:
        with pytest.raises(TransportFailure, match="invalid_path_parameter"):
            client.http(service, "GET", "/records/{id}", arguments)


def test_unapproved_destination_and_redirect_are_rejected(service: str) -> None:
    with pytest.raises(TransportFailure, match="destination_not_approved"):
        ExtensionTransport((), timeout=2, max_bytes=4096).http(service, "GET", "/", {})
    with pytest.raises(TransportFailure, match="redirect_rejected"):
        ExtensionTransport((service,), timeout=2, max_bytes=4096).http(service, "GET", "/redirect", {})


@pytest.mark.parametrize("suffix", ["?token=value", "#fragment", "/../lookup", "//other.example"])
def test_bad_endpoint_or_operation_path(service: str, suffix: str) -> None:
    with pytest.raises(TransportFailure):
        ExtensionTransport((service,), timeout=2, max_bytes=4096).http(service + suffix, "GET", "/", {})


def test_response_bound(service: str) -> None:
    with pytest.raises(TransportFailure, match="response_too_large"):
        ExtensionTransport((service,), timeout=2, max_bytes=8).http(service, "GET", "/lookup", {})


def test_real_mcp_handshake_discovery_and_call(service: str) -> None:
    client = ExtensionTransport((service,), timeout=2, max_bytes=4096)
    tools = client.discover(service)
    assert tools == [{"name": "lookup", "inputSchema": {"type": "object"}}]
    assert client.mcp(service, "lookup", {}, expected_schema={"type": "object"}) == {"company": "Example"}
    with pytest.raises(TransportFailure, match="tool_contract_changed"):
        client.mcp(service, "lookup", {}, expected_schema={"type": "object", "required": ["company"]})
