"""Real local HTTP extension calls with exact approval and one-shot receipts."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.gateway_extension_attempt_recovery import recover_remote_attempts
from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.gateway_extension_runtime import execute_saved
from onefoundry.gateway_extension_transport import ExtensionTransport
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied, ToolInputError, ToolOutcomeUnknown
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}


class _Handler(BaseHTTPRequestHandler):
    calls: list[str] = []

    def do_GET(self) -> None:
        type(self).calls.append(self.path)
        failed = "fail" in self.path
        body = json.dumps({"value": "ok" if not failed else "failed"}).encode()
        self.send_response(500 if failed else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def remote() -> Iterator[str]:
    _Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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


def _ready_extension(client: TestClient, gateway: ToolGateway, remote: str) -> tuple[UUID, UUID, UUID, str]:
    project = _post(client, "/api/projects", {"name": f"Remote extension {uuid4()}"})
    project_id = UUID(str(project["id"]))
    path = f"/api/projects/{project_id}"
    package = _post(
        client,
        f"{path}/extensions/packages",
        {
            "manifest": {
                "schema_version": 1,
                "id": "remote-fixture",
                "version": "1.0.0",
                "name": "Remote fixture",
                "connections": [
                    {
                        "id": "api",
                        "name": "API",
                        "kind": "http",
                        "operations": [
                            {"id": "lookup", "method": "GET", "path": "/lookup"},
                        ],
                    }
                ],
                "tools": [
                    {
                        "id": "lookup",
                        "description": "Read fixture",
                        "effect": "read",
                        "runtime": "http",
                        "connection": "api",
                        "operation": "lookup",
                        "parameters": {
                            "type": "object",
                            "properties": {"term": {"type": "string"}},
                            "required": ["term"],
                            "additionalProperties": False,
                        },
                        "returns": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ],
            },
            "files": {},
        },
    )
    package_id = package["id"]
    binding = _post(
        client,
        f"{path}/extensions/connections",
        {
            "package_id": package_id,
            "template_id": "api",
            "label": "Local fixture",
            "config": {"transport": "streamable_http", "endpoint": remote},
        },
    )
    review = _post(
        client,
        f"{path}/extensions/packages/{package_id}/activate",
        {
            "expected_sha256": package["sha256"],
            "expected_version": "1.0.0",
            "expected_revision": 1,
        },
    )
    _post(client, f"{path}/approvals/{review['approval_id']}/decision", {"decision": "approved"})
    enabled = client.patch(
        f"{path}/extensions/connections/{binding['id']}",
        json={"expected_version": 1, "enabled": True},
        headers=ORIGIN,
    )
    assert enabled.status_code == 200, enabled.text
    tool_id = canonical_tool_id("remote-fixture", "lookup")
    researcher = next(
        profile for profile in client.get(f"{path}/profiles").json() if profile["name"] == "Researcher"
    )
    profile_id = UUID(researcher["id"])
    grant = client.patch(
        f"{path}/profiles/{profile_id}",
        json={"allowed_tools": [tool_id]},
        headers=ORIGIN,
    )
    assert grant.status_code == 200, grant.text
    order = _post(
        client,
        f"{path}/work-orders",
        {
            "title": "Read fixture",
            "instructions": "Read the local fixture",
            "profile_id": str(profile_id),
        },
    )
    _post(client, f"{path}/work-orders/{order['id']}/ready", {})
    run = gateway.start_synthetic_text(project_id, UUID(str(order["id"])))
    return project_id, UUID(str(run["run_id"])), profile_id, tool_id


def test_remote_invocation_requires_exact_approval_and_never_replays_done(
    setup: tuple[TestClient, ToolGateway], remote: str
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready_extension(client, gateway, remote)
    assert tool_id in {item["id"] for item in gateway.available_role_tools(project_id, run_id, profile_id)}
    first = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-1", tool_id, {"term": "ok"}, tool_version=1
    )
    assert first["status"] == "approval_required"
    assert _Handler.calls == []
    with pytest.raises(ToolInputError):
        gateway.invoke_role_tool(project_id, run_id, profile_id, "bad-shape", tool_id, {}, tool_version=1)
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id, run_id, profile_id, "remote-1", tool_id, {"term": "changed"}, tool_version=1
        )
    same_pending = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-2", tool_id, {"term": "ok"}, tool_version=1
    )
    assert same_pending["invocation_id"] != first["invocation_id"]
    assert _Handler.calls == []
    decision = _post(
        client,
        f"/api/projects/{project_id}/approvals/{first['approval_id']}/decision",
        {"decision": "approved"},
    )
    assert decision["status"] == "approved"
    done = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-1", tool_id, {"term": "ok"}, tool_version=1
    )
    assert done["status"] == "done"
    assert done["result"] == {"value": "ok"}
    again = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-1", tool_id, {"term": "ok"}, tool_version=1
    )
    assert again["invocation_id"] == done["invocation_id"]
    assert again["status"] == "done"
    assert len(_Handler.calls) == 1
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{same_pending['approval_id']}/decision",
        {"decision": "approved"},
    )
    intentional_second = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-2", tool_id, {"term": "ok"}, tool_version=1
    )
    assert intentional_second["status"] == "done"
    assert len(_Handler.calls) == 2


def test_remote_failure_is_unknown_without_automatic_retry(
    setup: tuple[TestClient, ToolGateway], remote: str
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready_extension(client, gateway, remote)
    first = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-fail", tool_id, {"term": "fail"}, tool_version=1
    )
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{first['approval_id']}/decision",
        {"decision": "approved"},
    )
    with pytest.raises(ToolOutcomeUnknown):
        gateway.invoke_role_tool(
            project_id, run_id, profile_id, "remote-fail", tool_id, {"term": "fail"}, tool_version=1
        )
    assert len(_Handler.calls) == 1
    with pytest.raises(ToolOutcomeUnknown):
        gateway.invoke_role_tool(
            project_id, run_id, profile_id, "remote-fail", tool_id, {"term": "fail"}, tool_version=1
        )
    assert len(_Handler.calls) == 1


def test_exact_approved_call_can_resume_after_parent_run_finishes(
    setup: tuple[TestClient, ToolGateway], remote: str
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready_extension(client, gateway, remote)
    pending = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "remote-later", tool_id, {"term": "ok"}, tool_version=1
    )
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{pending['approval_id']}/decision",
        {"decision": "approved"},
    )
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status='done',finished_at=clock_timestamp(),"
                "outcome_reason='fixture_done' WHERE project_id=:p AND id=:run"
            ),
            {"p": project_id, "run": run_id},
        )
        connection.execute(
            text(
                "UPDATE work_orders SET status='done' WHERE project_id=:p AND id=("
                "SELECT work_order_id FROM runs WHERE project_id=:p AND id=:run)"
            ),
            {"p": project_id, "run": run_id},
        )
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id, run_id, profile_id, "new-agent-call", tool_id, {"term": "ok"}, tool_version=1
        )
    assert _Handler.calls == []
    resumed = execute_saved(gateway, project_id, UUID(pending["invocation_id"]))
    assert resumed["status"] == "done"
    assert resumed["result"] == {"value": "ok"}
    assert len(_Handler.calls) == 1


@pytest.mark.parametrize("sent", [False, True])
def test_interrupted_remote_claim_becomes_unknown_without_replay(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    monkeypatch: pytest.MonkeyPatch,
    sent: bool,
) -> None:
    class Interrupted(BaseException):
        pass

    original = ExtensionTransport.http

    def interrupt(self: ExtensionTransport, *args: object, **kwargs: object) -> dict[str, object]:
        if sent:
            original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise Interrupted

    client, gateway = setup
    project_id, run_id, profile_id, tool_id = _ready_extension(client, gateway, remote)
    pending = gateway.invoke_role_tool(
        project_id, run_id, profile_id, "crashed-remote", tool_id, {"term": "ok"}, tool_version=1
    )
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT started_at FROM extension_invocations WHERE id=:id"),
                {"id": pending["invocation_id"]},
            )
            is None
        )
    admin = create_engine(Settings(db_password=os.environ["DB_PASSWORD"]).database_url)
    try:
        with admin.begin() as connection:
            connection.execute(
                text("UPDATE extension_invocations SET created_at=created_at-interval '1 day' WHERE id=:id"),
                {"id": pending["invocation_id"]},
            )
    finally:
        admin.dispose()
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{pending['approval_id']}/decision",
        {"decision": "approved"},
    )
    monkeypatch.setattr(ExtensionTransport, "http", interrupt)
    with pytest.raises(Interrupted):
        execute_saved(gateway, project_id, UUID(pending["invocation_id"]))
    recover_remote_attempts(gateway)
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM extension_invocations WHERE id=:id"),
                {"id": pending["invocation_id"]},
            )
            == "running"
        )
    recover_remote_attempts(gateway, now=datetime.now(UTC) + timedelta(seconds=601))
    recover_remote_attempts(gateway, now=datetime.now(UTC) + timedelta(seconds=602))
    with pytest.raises(ToolOutcomeUnknown):
        execute_saved(gateway, project_id, UUID(pending["invocation_id"]))
    assert len(_Handler.calls) == int(sent)
