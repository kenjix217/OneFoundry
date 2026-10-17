"""Trusted role reads identify exact coding receipts without exposing artifacts."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_external_code_postgres import (
    client_for,
    heartbeat,
    journal_for,
    launch,
    launch_source,
    local_source,
    post,
    probe_heartbeat,
    ready_order,
    receipt,
)
from tests.test_role_tool_admission_postgres import _tool_events

pytest_plugins = ("tests.test_external_code_postgres",)
pytestmark = pytest.mark.usefixtures("isolated_model_secrets")
TOOL = "coding_evidence_v1"


def _reader(client: TestClient, gateway: ToolGateway, project_id: UUID) -> tuple[UUID, UUID]:
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    researcher = next(profile for profile in profiles if profile["name"] == "Researcher")
    profile_id = UUID(researcher["id"])
    allowed = client.patch(
        f"/api/projects/{project_id}/profiles/{profile_id}",
        json={"allowed_tools": [TOOL]},
        headers={"origin": "http://localhost:8765"},
    )
    assert allowed.status_code == 200, allowed.text
    order = post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {
            "title": "Inspect coding receipt",
            "instructions": "Use exact owner evidence",
            "profile_id": str(profile_id),
        },
    )
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]
    assert post(client, f"/api/projects/{project_id}/work-orders/{order_id}/ready").status_code == 200
    started = gateway.start_synthetic_text(project_id, UUID(order_id))
    return UUID(str(started["run_id"])), profile_id


def _gateway() -> ToolGateway:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )
    return ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)


def _call(
    gateway: ToolGateway, project_id: UUID, reader_run: UUID, profile_id: UUID, target_run: UUID
) -> dict[str, object]:
    return gateway.invoke_role_tool(
        project_id,
        reader_run,
        profile_id,
        uuid4().hex,
        TOOL,
        {"run_id": str(target_run)},
        tool_version=1,
    )


def test_coding_evidence_changes_from_queued_to_reconciled_without_raw_artifacts(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_raw, order_id = ready_order(client)
        project_id = UUID(project_raw)
        launched_response = launch(client, project_raw, order_id)
        assert launched_response.status_code == 201, launched_response.text
        launched = launched_response.json()
        target_run = UUID(launched["run"]["id"])
        attempt_id = launched["attempts"][0]["id"]
        gateway = _gateway()
        try:
            reader_run, profile_id = _reader(client, gateway, project_id)
            assert TOOL in {
                item["id"] for item in gateway.available_role_tools(project_id, reader_run, profile_id)
            }
            queued = _call(gateway, project_id, reader_run, profile_id, target_run)["result"]
            assert queued["found"] is True  # type: ignore[index]
            assert queued["attempts"][0]["attempt_id"] == attempt_id  # type: ignore[index]
            assert queued["attempts"][0]["status"] == "queued"  # type: ignore[index]
            assert queued["attempts"][0]["tests"] == []  # type: ignore[index]
            assert queued["attempts"][0]["tests_source"] == "none"  # type: ignore[index]
            assert queued["attempts"][0]["receipt_event_verified"] is False  # type: ignore[index]
            assert queued["attempts"][0]["verified_outcome"] is None  # type: ignore[index]
            assert queued["attempts"][0]["live_isolation_proven"] is False  # type: ignore[index]

            journal_for(spool, launched, session_id="session-123", synthetic=True)
            receipt(spool, launched)
            saved = post(client, f"/api/projects/{project_id}/runs/{target_run}/external-code/reconcile")
            assert saved.status_code == 200, saved.text
            done = _call(gateway, project_id, reader_run, profile_id, target_run)["result"]
            attempt = done["attempts"][0]  # type: ignore[index]
            assert attempt["status"] == "done"
            assert attempt["baseline_commit"] == "a" * 40
            assert attempt["candidate_commit"] == "b" * 40
            assert attempt["diff_sha256"] == saved.json()["attempts"][0]["diff_sha256"]
            assert attempt["receipt_sha256"] == saved.json()["attempts"][0]["receipt_sha256"]
            assert attempt["tests"] == [{"name": "fixture_test", "status": "passed", "exit_code": 0}]
            assert attempt["tests_source"] == "reconciled_runner_receipt"
            assert attempt["receipt_event_verified"] is True
            assert attempt["verified_outcome"] == "done"
            assert attempt["independent_qa_verified"] is False
            assert attempt["live_isolation_proven"] is False
            assert {item["kind"] for item in attempt["artifacts"]} == {"diff", "log"}
            encoded = json.dumps(done)
            assert "session-123" not in encoded
            assert "change.diff" not in encoded
            assert "diff --git" not in encoded
            assert "session-123" not in json.dumps(_tool_events(gateway, project_id, reader_run))
        finally:
            gateway.engine.dispose()


def test_coding_evidence_is_project_scoped_and_permission_revocation_is_immediate(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_raw, order_id = ready_order(client)
        project_id = UUID(project_raw)
        launched = launch(client, project_raw, order_id).json()
        foreign_project, foreign_order = ready_order(client)
        foreign_run = UUID(launch(client, foreign_project, foreign_order).json()["run"]["id"])
        gateway = _gateway()
        try:
            reader_run, profile_id = _reader(client, gateway, project_id)
            hidden = _call(gateway, project_id, reader_run, profile_id, uuid4())["result"]
            assert hidden["found"] is False  # type: ignore[index]
            assert hidden["attempts"] == []  # type: ignore[index]
            foreign = _call(gateway, project_id, reader_run, profile_id, foreign_run)["result"]
            assert foreign["found"] is False  # type: ignore[index]
            with pytest.raises(ToolDenied):
                gateway.invoke_role_tool(
                    project_id, reader_run, profile_id, uuid4().hex, TOOL, {"run_id": "bad"}, tool_version=1
                )
            revoked = client.patch(
                f"/api/projects/{project_id}/profiles/{profile_id}",
                json={"allowed_tools": []},
                headers={"origin": "http://localhost:8765"},
            )
            assert revoked.status_code == 200
            assert TOOL not in {
                item["id"] for item in gateway.available_role_tools(project_id, reader_run, profile_id)
            }
            with pytest.raises(ToolDenied):
                _call(gateway, project_id, reader_run, profile_id, UUID(launched["run"]["id"]))
        finally:
            gateway.engine.dispose()


def test_coding_evidence_keeps_unknown_outcome_unproved(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_raw, order_id = ready_order(client)
        project_id = UUID(project_raw)
        launched = launch(client, project_raw, order_id).json()
        target_run = UUID(launched["run"]["id"])
        receipt(spool, launched, valid=False)
        reconciled = post(client, f"/api/projects/{project_id}/runs/{target_run}/external-code/reconcile")
        assert reconciled.status_code == 200
        gateway = _gateway()
        try:
            reader_run, profile_id = _reader(client, gateway, project_id)
            evidence = _call(gateway, project_id, reader_run, profile_id, target_run)["result"]
            assert evidence["run_status"] == "unknown"  # type: ignore[index]
            attempt = evidence["attempts"][0]  # type: ignore[index]
            assert attempt["status"] == "unknown"
            assert attempt["receipt_sha256"] is None
            assert attempt["tests_source"] == "none"
            assert attempt["receipt_event_verified"] is False
            assert attempt["verified_outcome"] is None
            assert attempt["tests"] == []
            assert attempt["artifacts"] == []
            assert attempt["independent_qa_verified"] is False
        finally:
            gateway.engine.dispose()


def test_coding_evidence_keeps_one_snapshot_during_concurrent_status_commit(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_raw, order_id = ready_order(client)
        project_id = UUID(project_raw)
        launched = launch(client, project_raw, order_id).json()
        target_run = UUID(launched["run"]["id"])
        gateway = _gateway()
        committed = False

        def commit_after_run_read(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal committed
            if not committed and "SELECT status FROM runs WHERE" in statement:
                committed = True
                with gateway.engine.begin() as concurrent:
                    concurrent.exec_driver_sql("SET LOCAL lock_timeout='2s'")
                    concurrent.execute(
                        text("UPDATE runs SET status='done' WHERE project_id=:project AND id=:run"),
                        {"project": project_id, "run": target_run},
                    )
                    concurrent.execute(
                        text(
                            "UPDATE external_code_attempts SET status='done' "
                            "WHERE project_id=:project AND run_id=:run"
                        ),
                        {"project": project_id, "run": target_run},
                    )

        try:
            reader_run, profile_id = _reader(client, gateway, project_id)
            event.listen(gateway.engine, "after_cursor_execute", commit_after_run_read)
            try:
                before = _call(gateway, project_id, reader_run, profile_id, target_run)["result"]
            finally:
                event.remove(gateway.engine, "after_cursor_execute", commit_after_run_read)
            assert committed is True
            assert before["run_status"] == "running"  # type: ignore[index]
            assert before["attempts"][0]["status"] == "queued"  # type: ignore[index]
            after = _call(gateway, project_id, reader_run, profile_id, target_run)["result"]
            assert after["run_status"] == "done"  # type: ignore[index]
            assert after["attempts"][0]["status"] == "done"  # type: ignore[index]
            assert after["attempts"][0]["receipt_event_verified"] is False  # type: ignore[index]
            assert after["attempts"][0]["verified_outcome"] is None  # type: ignore[index]
        finally:
            gateway.engine.dispose()


def test_coding_evidence_keeps_saved_local_source_revision(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    probe_heartbeat(spool)
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        project_raw, order_id = ready_order(client)
        project_id = UUID(project_raw)
        source = local_source(client, project_raw, root)
        launched_response = launch_source(client, project_raw, order_id, source["id"])
        assert launched_response.status_code == 201, launched_response.text
        target_run = UUID(launched_response.json()["run"]["id"])
        gateway = _gateway()
        try:
            reader_run, profile_id = _reader(client, gateway, project_id)
            evidence = _call(gateway, project_id, reader_run, profile_id, target_run)["result"]
            attempt = evidence["attempts"][0]  # type: ignore[index]
            assert attempt["source_kind"] == "local"
            assert attempt["source_id"] == source["id"]
            assert attempt["source_revision"] == source["revision"]
            assert attempt["source_branch"] == source["branch"]
            assert attempt["source_tree"] is None
            assert attempt["status"] == "queued"
        finally:
            gateway.engine.dispose()
