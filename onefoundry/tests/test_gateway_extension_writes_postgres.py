"""Real API and MCP write calls are exact, approved, and never replayed."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import create_engine

from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied, ToolOutcomeUnknown
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}
INPUT = {
    "type": "object",
    "properties": {"item": {"type": "string"}},
    "required": ["item"],
    "additionalProperties": False,
}
OUTPUT = {
    "type": "object",
    "properties": {"accepted": {"type": "string"}},
    "required": ["accepted"],
    "additionalProperties": False,
}


class _WriteHandler(BaseHTTPRequestHandler):
    effects: list[tuple[str, str]] = []
    drift = False

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        status = 200
        if self.path == "/write" or self.path == "/write-fail":
            item = payload["item"]
            type(self).effects.append(("api", item))
            status = 500 if self.path.endswith("fail") else 200
            result: dict[str, object] = {"accepted": item}
        elif self.path == "/mcp":
            method = payload["method"]
            if method == "initialize":
                result = {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "serverInfo": {"name": "fixture", "version": "1.0.0"},
                }
            elif method == "notifications/initialized":
                result = {}
            elif method == "tools/list":
                schema = {"type": "object", "properties": {}} if type(self).drift else INPUT
                result = {"tools": [{"name": "write_item", "inputSchema": schema}]}
            elif method == "tools/call":
                item = payload["params"]["arguments"]["item"]
                type(self).effects.append(("mcp", item))
                result = {"structuredContent": {"accepted": item}}
            else:
                status, result = 400, {}
            result = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
        else:
            status, result = 404, {}
        body = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def remote() -> Iterator[str]:
    _WriteHandler.effects = []
    _WriteHandler.drift = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WriteHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def setup(tmp_path: Path, remote: str) -> Iterator[tuple[TestClient, ToolGateway]]:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        extension_allowed_origins=[remote],
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            yield client, gateway
    finally:
        engine.dispose()


def _post(client: TestClient, path: str, body: dict[str, object]) -> dict[str, object]:
    response = client.post(path, json=body, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()  # type: ignore[no-any-return]


def _ready(
    client: TestClient, gateway: ToolGateway, remote: str, *, kind: str, path: str = "/write"
) -> tuple[UUID, UUID, UUID, str]:
    project = _post(client, "/api/projects", {"name": f"Write fixture {uuid4()}"})
    project_id = UUID(str(project["id"]))
    base = f"/api/projects/{project_id}"
    operation = "write_item"
    package = _post(
        client,
        f"{base}/extensions/packages",
        {
            "manifest": {
                "schema_version": 1,
                "id": "write-fixture",
                "version": "1.0.0",
                "name": "Write fixture",
                "connections": [
                    {
                        "id": "target",
                        "name": "Target",
                        "kind": kind,
                        "operations": [
                            {"id": operation, **({"method": "POST", "path": path} if kind != "mcp" else {})}
                        ],
                    }
                ],
                "tools": [
                    {
                        "id": operation,
                        "description": "Write one item",
                        "effect": "write",
                        "runtime": "mcp" if kind == "mcp" else "http",
                        "connection": "target",
                        "operation": operation,
                        "parameters": INPUT,
                        "returns": OUTPUT,
                    }
                ],
            },
            "files": {},
        },
    )
    bound = _post(
        client,
        f"{base}/extensions/connections",
        {
            "package_id": package["id"],
            "template_id": "target",
            "label": "Local writer",
            "config": {
                "transport": "streamable_http",
                "endpoint": remote + ("/mcp" if kind == "mcp" else ""),
            },
        },
    )
    review = _post(
        client,
        f"{base}/extensions/packages/{package['id']}/activate",
        {
            "expected_sha256": package["sha256"],
            "expected_version": "1.0.0",
            "expected_revision": 1,
        },
    )
    _post(client, f"{base}/approvals/{review['approval_id']}/decision", {"decision": "approved"})
    enabled = client.patch(
        f"{base}/extensions/connections/{bound['id']}",
        json={"expected_version": 1, "enabled": True},
        headers=ORIGIN,
    )
    assert enabled.status_code == 200, enabled.text
    tool_id = canonical_tool_id("write-fixture", operation)
    profile = next(row for row in client.get(f"{base}/profiles").json() if row["name"] == "Researcher")
    profile_id = UUID(profile["id"])
    granted = client.patch(
        f"{base}/profiles/{profile_id}",
        json={"allowed_tools": [tool_id]},
        headers=ORIGIN,
    )
    assert granted.status_code == 200, granted.text
    order = _post(
        client,
        f"{base}/work-orders",
        {
            "title": "Write fixture",
            "instructions": "Write the fixture item",
            "profile_id": str(profile_id),
        },
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready", {})
    run = gateway.start_synthetic_text(project_id, UUID(str(order["id"])))
    return project_id, UUID(str(run["run_id"])), profile_id, tool_id


@pytest.mark.parametrize("kind", ["http", "mcp", "webhook"])
def test_write_requires_approval_and_exact_call_replay(
    setup: tuple[TestClient, ToolGateway], remote: str, kind: str
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready(client, gateway, remote, kind=kind)
    first = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "write-1", tool_id, {"item": "same"}, tool_version=1
    )
    second = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "write-2", tool_id, {"item": "same"}, tool_version=1
    )
    assert first["status"] == second["status"] == "approval_required"
    assert first["invocation_id"] != second["invocation_id"]
    assert first["approval_id"] != second["approval_id"]
    assert _WriteHandler.effects == []
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id, run_id, profile_id, "write-1", tool_id, {"item": "changed"}, tool_version=1
        )
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{first['approval_id']}/decision",
        {"decision": "approved"},
    )
    done = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "write-1", tool_id, {"item": "same"}, tool_version=1
    )
    assert done["status"] == "done"
    assert done["result"] == {"accepted": "same"}
    replay = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "write-1", tool_id, {"item": "same"}, tool_version=1
    )
    assert replay["invocation_id"] == first["invocation_id"]
    assert replay["status"] == "done"
    assert _WriteHandler.effects == [("mcp" if kind == "mcp" else "api", "same")]
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{second['approval_id']}/decision",
        {"decision": "approved"},
    )
    second_done = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "write-2", tool_id, {"item": "same"}, tool_version=1
    )
    assert second_done["status"] == "done"
    assert _WriteHandler.effects == [("mcp" if kind == "mcp" else "api", "same")] * 2


def test_write_dependency_failure_is_unknown_without_retry(
    setup: tuple[TestClient, ToolGateway], remote: str
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready(client, gateway, remote, kind="http", path="/write-fail")
    pending = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "write-fail", tool_id, {"item": "one"}, tool_version=1
    )
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{pending['approval_id']}/decision",
        {"decision": "approved"},
    )
    for _ in range(2):
        with pytest.raises(ToolOutcomeUnknown):
            gateway.invoke_role_tool(
                project_id, run_id, profile_id, "write-fail", tool_id, {"item": "one"}, tool_version=1
            )
    assert _WriteHandler.effects == [("api", "one")]


def test_mcp_schema_drift_and_profile_scope_deny_before_effect(
    setup: tuple[TestClient, ToolGateway], remote: str
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready(client, gateway, remote, kind="mcp")
    other_profile = next(
        UUID(row["id"])
        for row in client.get(f"/api/projects/{project_id}/profiles").json()
        if row["name"] == "GTM"
    )
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id,
            run_id,
            other_profile,
            "other-profile",
            tool_id,
            {"item": "one"},
            tool_version=1,
        )
    assert _WriteHandler.effects == []
    pending = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "drift", tool_id, {"item": "one"}, tool_version=1
    )
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{pending['approval_id']}/decision",
        {"decision": "approved"},
    )
    _WriteHandler.drift = True
    with pytest.raises(ToolOutcomeUnknown):
        gateway.invoke_role_tool(
            project_id, run_id, profile_id, "drift", tool_id, {"item": "one"}, tool_version=1
        )
    assert _WriteHandler.effects == []
