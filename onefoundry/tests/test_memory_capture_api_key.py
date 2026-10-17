"""Synthetic selective capture: scopes, exact preview, bounded inputs and no ambient import."""

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from onefoundry.db_roles import RuntimeRoleSettings
from onefoundry.memory_plugin_api_key import MemoryApiKeyVerifier
from onefoundry.memory_plugin_capture_db_role import provision_memory_capture_role
from onefoundry.memory_plugin_capture_preview import CapturePreviews
from onefoundry.memory_plugin_capture_settings import MemoryCaptureDatabaseSettings
from onefoundry.memory_plugin_contracts import MemoryCapture
from onefoundry.memory_plugin_gateway import MemoryFacadeError, MemoryPluginGateway
from onefoundry.memory_plugin_service import create_memory_plugin_app
from onefoundry.memory_plugin_settings import MemoryGrant
from onefoundry.settings import Settings
from tests.memory_plugin_fixtures import policy


def selection(project: str, **changes: Any) -> dict[str, Any]:
    return {
        "project_id": project,
        "client_capture_id": str(uuid4()),
        "source_ref": "user-selected synthetic meeting excerpt",
        "authorized_company_content": True,
        "messages": [
            {"speaker": "user", "text": "Copper renewals occur annually", "sent_at": "2026-10-01T12:00:00Z"}
        ],
        "summary": "Copper renewals occur annually",
        "scope": "project",
        **changes,
    }


def setup(tmp_path: Path, *, company: bool = False) -> tuple[Any, Any, str, str, Any, Any]:
    grant = MemoryGrant(
        subject="synthetic-capture",
        client_id="synthetic-selected-capture",
        actor_id="external-memory:fixture-capture",
        project_ids=(uuid4(),),
        capture=True,
        company_read=company,
        company_capture=company,
    )
    records = tmp_path / "keys.json"
    read, write = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    rows = []
    for key, scopes in ((read, None), (write, ["memory.read", "memory.capture"])):
        row = {
            "sha256": hashlib.sha256(key.encode()).hexdigest(),
            "subject": grant.subject,
            "client_id": grant.client_id,
            "expires_at": int(time.time()) + 600,
        }
        if scopes:
            row["scopes"] = scopes
        rows.append(row)
    records.write_text(json.dumps(rows))
    records.chmod(0o600)
    configured = policy(
        auth_mode="api_key",
        api_key_records_path=records,
        oauth_issuer=None,
        oauth_public_key_path=None,
        public_url="http://127.0.0.1:9877/mcp",
        grants=[grant],
        max_calls_per_minute=120,
    )
    reader, writer = MagicMock(), MagicMock()
    reader.retrieve_memory.return_value = []
    gateway = MemoryPluginGateway(
        configured, {grant.actor_id: reader}, capture_gateways={grant.actor_id: writer}
    )
    verifier = MemoryApiKeyVerifier(configured)
    app = create_memory_plugin_app(configured, gateway, verifier)
    return configured, app, read, write, reader, writer


def rpc(client: TestClient, key: str, method: str, params: Any = None) -> Any:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})},
        headers={
            "authorization": "Bearer " + key,
            "accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def call(client: TestClient, key: str, action: str, arguments: dict[str, Any]) -> Any:
    return rpc(client, key, "tools/call", {"name": action, "arguments": arguments})


def receipt(project: str, actor: str) -> dict[str, Any]:
    source, result, candidate = uuid4(), uuid4(), uuid4()
    created = datetime.now(UTC)
    return {
        "source_id": source,
        "source_sha256": "a" * 64,
        "result": {
            "id": result,
            "project_id": project,
            "actor_id": actor,
            "run_id": None,
            "summary": "Copper renewals occur annually",
            "scope": "project",
            "source_kind": "conversation_capture",
            "source_capture_id": source,
            "created_at": created,
        },
        "candidate": {
            "id": candidate,
            "project_id": project,
            "actor_id": actor,
            "source_result_id": result,
            "summary": "Copper renewals occur annually",
            "scope": "project",
            "status": "pending",
            "created_at": created,
            "reviewed_at": None,
            "reviewed_by_actor_id": None,
        },
    }


def test_old_read_key_keeps_two_tools_and_cannot_preview_or_write(tmp_path: Path) -> None:
    configured, app, read, write, reader, writer = setup(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:9877") as client:
        assert {t["name"] for t in rpc(client, read, "tools/list")["tools"]} == {
            "memory_search",
            "memory_read",
        }
        names = {t["name"] for t in rpc(client, write, "tools/list")["tools"]}
        assert names == {"memory_search", "memory_read", "memory_capture_preview", "memory_capture"}
        for action in ("memory_capture_preview", "memory_capture"):
            result = call(client, read, action, selection(str(configured.grants[0].project_ids[0])))
            assert result["isError"] and result["content"][0]["text"] == "authorization_denied"
    writer.capture_company_memory.assert_not_called()
    reader.capture_company_memory.assert_not_called()


def test_exact_selected_preview_then_draft_receipt_and_duplicate_retry(tmp_path: Path) -> None:
    configured, app, _read, write, reader, writer = setup(tmp_path)
    grant = configured.grants[0]
    project = str(grant.project_ids[0])
    chosen = selection(project)
    writer.capture_company_memory.return_value = receipt(project, grant.actor_id)
    with TestClient(app, base_url="http://127.0.0.1:9877") as client:
        preview = call(client, write, "memory_capture_preview", chosen)["structuredContent"]
        assert preview["selection"] == MemoryCapture.model_validate(chosen).model_dump(mode="json")
        assert preview["company_content_stored"] is False and preview["source_archive_retained_on_rejection"]
        writer.capture_company_memory.assert_not_called()
        data = {**chosen, "preview_sha256": preview["preview_sha256"]}
        result = call(client, write, "memory_capture", data)["structuredContent"]
        retry = call(client, write, "memory_capture", data)["structuredContent"]
        assert result == retry and result["review_required"]
        assert result["selection_sha256"] == preview["preview_sha256"]
        assert result["capture"]["candidate"]["status"] == "pending"
        assert (
            writer.capture_company_memory.call_args.args[1].messages[0].text == chosen["messages"][0]["text"]
        )
        assert (
            call(client, write, "memory_search", {"project_id": project})["structuredContent"]["items"] == []
        )
    reader.capture_company_memory.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"summary": "Changed selected fact"},
        {"source_ref": "Changed source"},
        {"client_capture_id": str(uuid4())},
    ],
)
def test_changed_selection_requires_new_preview(tmp_path: Path, change: dict[str, Any]) -> None:
    configured, app, _read, write, _reader, writer = setup(tmp_path)
    data = selection(str(configured.grants[0].project_ids[0]))
    with TestClient(app, base_url="http://127.0.0.1:9877") as client:
        preview = call(client, write, "memory_capture_preview", data)["structuredContent"]
        result = call(
            client, write, "memory_capture", {**data, **change, "preview_sha256": preview["preview_sha256"]}
        )
        assert result["content"][0]["text"] == "capture_preview_required"
    writer.capture_company_memory.assert_not_called()


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"project_id": str(uuid4())}, "authorization_denied"),
        ({"scope": "company"}, "authorization_denied"),
        ({"authorized_company_content": False}, "invalid_input"),
        ({"ambient_chat": "not imported"}, "invalid_input"),
        (
            {
                "messages": [
                    {"speaker": "system", "text": "Hidden instructions", "sent_at": "2026-10-01T12:00:00Z"}
                ]
            },
            "invalid_input",
        ),
        ({"summary": "password=synthetic-unapproved-input"}, "invalid_input"),
        ({"messages": []}, "invalid_input"),
    ],
)
def test_invalid_or_unapproved_preview_never_writes(
    tmp_path: Path, changes: dict[str, Any], code: str
) -> None:
    configured, app, _read, write, reader, writer = setup(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:9877") as client:
        result = call(
            client,
            write,
            "memory_capture_preview",
            selection(str(configured.grants[0].project_ids[0]), **changes),
        )
        assert result["isError"] and result["content"][0]["text"] == code
    reader.capture_company_memory.assert_not_called()
    writer.capture_company_memory.assert_not_called()


def test_preview_ttl_capacity_and_actor_binding() -> None:
    clock = [0.0]
    registry = CapturePreviews(30, 1, clock=lambda: clock[0])
    selected = MemoryCapture.model_validate(selection(str(uuid4())))
    first = registry.preview("actor-a", selected)["preview_sha256"]
    assert registry.matches("actor-a", selected, first)
    assert not registry.matches("actor-b", selected, first)
    clock[0] = 30
    assert not registry.matches("actor-a", selected, first)
    clock[0] = 31
    registry.preview("actor-a", selected)
    registry.preview("actor-a", selected.model_copy(update={"client_capture_id": uuid4()}))
    assert not registry.matches("actor-a", selected, first)


def test_capture_config_missing_invalid_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_CAPTURE_DB_USER", raising=False)
    monkeypatch.delenv("MEMORY_CAPTURE_DB_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        MemoryCaptureDatabaseSettings()  # type: ignore[call-arg]
    monkeypatch.setenv("MEMORY_CAPTURE_DB_USER", "synthetic_capture")
    monkeypatch.setenv("MEMORY_CAPTURE_DB_PASSWORD", secrets.token_urlsafe(32))
    assert MemoryCaptureDatabaseSettings().db_user == "synthetic_capture"  # type: ignore[call-arg]
    assert MemoryCaptureDatabaseSettings(db_user="explicit_capture").db_user == "explicit_capture"  # type: ignore[call-arg]
    for name in ("pg_bad", "UPPERCASE", "unsafe space"):
        with pytest.raises(ValidationError):
            MemoryCaptureDatabaseSettings(db_user=name)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        MemoryCaptureDatabaseSettings(db_password=" ")  # type: ignore[call-arg]


def test_unapproved_capture_key_scope_is_rejected_at_startup(tmp_path: Path) -> None:
    configured, _app, read, write, _reader, _writer = setup(tmp_path)
    verifier = MemoryApiKeyVerifier(configured)
    read_token, capture_token = (
        anyio.run(verifier.verify_token, read),
        anyio.run(verifier.verify_token, write),
    )
    assert read_token is not None and read_token.scopes == ["memory.read"]
    assert capture_token is not None and capture_token.scopes == ["memory.read", "memory.capture"]
    denied = configured.model_copy(
        update={"grants": (configured.grants[0].model_copy(update={"capture": False}),)}
    )
    with pytest.raises(ValueError, match="capture grant"):
        MemoryApiKeyVerifier(denied)


def test_company_capture_needs_its_own_grant_and_preserves_selection(tmp_path: Path) -> None:
    configured, app, _read, write, _reader, writer = setup(tmp_path, company=True)
    grant = configured.grants[0]
    chosen = selection(str(grant.project_ids[0]), scope="company")
    saved = receipt(str(grant.project_ids[0]), grant.actor_id)
    saved["result"]["scope"] = saved["candidate"]["scope"] = "company"
    writer.capture_company_memory.return_value = saved
    with TestClient(app, base_url="http://127.0.0.1:9877") as client:
        preview = call(client, write, "memory_capture_preview", chosen)["structuredContent"]
        result = call(
            client, write, "memory_capture", {**chosen, "preview_sha256": preview["preview_sha256"]}
        )["structuredContent"]
        assert result["capture"]["candidate"]["scope"] == "company"


def test_company_read_does_not_authorize_company_capture(tmp_path: Path) -> None:
    configured, _app, _read, _write, reader, writer = setup(tmp_path, company=True)
    grant = configured.grants[0].model_copy(update={"company_capture": False})
    configured = configured.model_copy(update={"grants": (grant,)})
    gateway = MemoryPluginGateway(
        configured, {grant.actor_id: reader}, capture_gateways={grant.actor_id: writer}
    )
    project = str(grant.project_ids[0])
    assert gateway.invoke(grant, "memory_search", {"project_id": project, "scope": "company"})["items"] == []
    assert (
        gateway.invoke(grant, "memory_capture_preview", selection(project))["company_content_stored"] is False
    )
    with pytest.raises(MemoryFacadeError, match="authorization_denied"):
        gateway.invoke(grant, "memory_capture_preview", selection(project, scope="company"))
    writer.capture_company_memory.assert_not_called()


def test_submission_without_preview_and_missing_writer_fail_closed(tmp_path: Path) -> None:
    configured, app, _read, write, reader, writer = setup(tmp_path)
    selected = selection(str(configured.grants[0].project_ids[0]))
    with TestClient(app, base_url="http://127.0.0.1:9877") as client:
        assert call(client, write, "memory_capture", selected)["isError"]
        unmatched = call(client, write, "memory_capture", {**selected, "preview_sha256": "a" * 64})
        assert unmatched["content"][0]["text"] == "capture_preview_required"
    writer.capture_company_memory.assert_not_called()
    gateway = MemoryPluginGateway(configured, {configured.grants[0].actor_id: reader})
    preview = gateway.invoke(configured.grants[0], "memory_capture_preview", selected)
    with pytest.raises(MemoryFacadeError, match="dependency_unavailable"):
        gateway.invoke(
            configured.grants[0], "memory_capture", {**selected, "preview_sha256": preview["preview_sha256"]}
        )


@pytest.mark.parametrize("seconds", [29, 901])
def test_preview_configuration_bounds(tmp_path: Path, seconds: int) -> None:
    configured, _app, _read, _write, _reader, _writer = setup(tmp_path)
    with pytest.raises(ValidationError):
        type(configured).model_validate({**configured.model_dump(), "capture_preview_seconds": seconds})


@pytest.mark.parametrize("deadline", [0, 1, 61, True])
def test_capture_provision_invalid_deadline_never_contacts_database(deadline: int) -> None:
    with patch("onefoundry.memory_plugin_capture_db_role.connect") as connect:
        with pytest.raises(ValueError, match="deadline"):
            provision_memory_capture_role(
                Settings(db_password=secrets.token_urlsafe(32)),
                RuntimeRoleSettings(
                    app_db_user="synthetic_capture", app_db_password=secrets.token_urlsafe(32)
                ),
                deadline_seconds=deadline,
            )
        connect.assert_not_called()
