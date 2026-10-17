"""PostgreSQL owner-path tests for the delegated coding boundary."""

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Literal, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.input_file_bytes import InputFileBytes
from onefoundry.input_file_worker import process_one
from onefoundry.main import create_app
from onefoundry.settings import Settings
from runner.external_cli import CliRequest, CliResult
from runner.external_launch_journal import record_prelaunch, record_session
from runner.live_progress_spool import LiveProgressJournal
from runner.provider_progress import AGENT_UPDATE_TEXT, ProviderProgress


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    external = tmp_path / "external"
    external.mkdir()
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (external / name).mkdir()
    return external


@pytest.fixture
def isolated_model_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("MODEL_SECRET_ROOT", str(root))


def client_for(
    spool: Path,
    *,
    enabled: bool,
    local_root: Path | None = None,
    offline_enabled: bool = False,
) -> TestClient:
    input_root = spool.parent / "input-files"
    input_root.mkdir(mode=0o700, exist_ok=True)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        runner_spool_root=str(spool.parent),
        input_file_root=str(input_root),
        external_code_enabled=enabled,
        team_repository_read_enabled=False,
        offline_source_probe_enabled=offline_enabled,
        local_source_root=str(local_root) if local_root else "",
    )
    return TestClient(create_app(settings), base_url="http://localhost:8765")


def post(client: TestClient, path: str, data: dict[str, Any] | None = None) -> Any:
    return client.post(path, json=data, headers={"origin": "http://localhost:8765"})


def ready_order(client: TestClient) -> tuple[str, str]:
    project = post(client, "/api/projects", {"name": "External " + str(uuid4())}).json()
    project_id = project["id"]
    profile = next(
        item
        for item in client.get(f"/api/projects/{project_id}/profiles").json()
        if item["name"] == "Engineer"
    )
    order = post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {
            "title": "Repair calculator",
            "instructions": "Fix the fixture calculator and test it.",
            "profile_id": profile["id"],
        },
    ).json()
    assert post(client, f"/api/projects/{project_id}/work-orders/{order['id']}/ready").status_code == 200
    return project_id, order["id"]


def heartbeat(
    spool: Path,
    *,
    age_seconds: int = 0,
    isolated: bool = False,
    mode: str = "trusted_local_cli_v1",
    auth_ready: bool = True,
    available: bool = True,
) -> None:
    value = {
        "schema_version": 1,
        "observed_at": (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat(),
        "mode": mode,
        "isolated": isolated,
        "auth_ready": auth_ready,
        "adapters": [
            {
                "adapter_id": adapter,
                "available": available,
                "reason": None if available else "Adapter is unavailable.",
            }
            for adapter in ("codex_cli", "claude_code")
        ],
    }
    (spool / "capabilities.json").write_text(json.dumps(value))


def probe_heartbeat(spool: Path, *, age_seconds: int = 0, isolated: bool = True) -> None:
    (spool / "probe_capabilities.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "observed_at": (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat(),
                "isolated": isolated,
                "source_root_ready": True,
                "available": isolated,
                "reason": None if isolated else "isolation_unavailable",
            }
        )
    )


def launch(
    client: TestClient,
    project_id: str,
    order_id: str,
    adapter_id: Literal["codex_cli", "claude_code"] = "codex_cli",
) -> Any:
    return post(
        client,
        f"/api/projects/{project_id}/work-orders/{order_id}/external-code/runs",
        {"adapter_id": adapter_id},
    )


def test_live_progress_is_claim_bound_and_project_scoped(spool: Path) -> None:
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        heartbeat(spool)
        launched = launch(client, project_id, order_id)
        assert launched.status_code == 201, launched.text
        run_id = launched.json()["run"]["id"]
        attempt_id = launched.json()["attempts"][0]["id"]
        manifest = json.loads((spool / "jobs" / f"{attempt_id}.json").read_text())
        route = f"/api/projects/{project_id}/runs/{run_id}/external-code/attempts/{attempt_id}/progress"
        empty = client.get(route)
        assert empty.status_code == 200
        assert empty.json()["items"] == []
        claim = spool / "claims" / f"{attempt_id}.claim"
        claim.write_text(attempt_id)
        claim.chmod(0o600)
        with LiveProgressJournal.create(
            spool, project_id, run_id, attempt_id, manifest["request_sha256"]
        ) as journal:
            journal.append(ProviderProgress("agent_update", AGENT_UPDATE_TEXT))
            observed = client.get(route)
            assert observed.status_code == 200, observed.text
            assert observed.json()["latest_sequence"] == 1
            assert observed.json()["terminal"] is False
            assert observed.json()["items"][0]["text"] == AGENT_UPDATE_TEXT
            assert client.get(route + "?after=1").json()["items"] == []
            assert client.get(route + "?after=25").status_code == 422
            wrong_attempt = route.replace(attempt_id, str(uuid4()))
            assert client.get(wrong_attempt).status_code == 404
            wrong_project = route.replace(project_id, str(uuid4()))
            assert client.get(wrong_project).status_code == 404
        path = spool / "claims" / f"{attempt_id}.progress.jsonl"
        path.write_bytes(path.read_bytes()[:-1])
        assert client.get(route).status_code == 503


def test_coding_manifest_includes_verified_project_and_work_order_text(spool: Path) -> None:
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        root = f"/api/projects/{project_id}/input-files"
        uploaded_ids: list[str] = []
        for scope, name, content in (
            ("project", "company.md", b"Serve rural districts."),
            ("work_order", "task.md", b"Start with outreach interviews."),
        ):
            query = f"?scope={scope}" + (f"&work_order_id={order_id}" if scope == "work_order" else "")
            response = client.post(
                root + query,
                content=content,
                headers={
                    "origin": "http://localhost:8765",
                    "X-File-Name": name,
                    "Content-Type": "application/octet-stream",
                },
            )
            assert response.status_code == 201, response.text
            uploaded_ids.append(response.json()["id"])
        settings = Settings(
            db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
            db_password=os.environ["APP_DB_PASSWORD"],
            input_file_root=str(spool.parent / "input-files"),
        )
        engine = create_engine(settings.database_url)
        storage = InputFileBytes(settings.input_file_root)
        # The worker claims the oldest pending file across all projects. Earlier
        # tests can leave unrelated uploads pending in this shared test database.
        with engine.connect() as connection:
            pending_count = connection.scalar(text("SELECT count(*) FROM input_files WHERE status='pending'"))
        statuses: list[str] = []
        for _ in range(pending_count):
            assert process_one(engine, storage, settings)
            with engine.connect() as connection:
                statuses = cast(
                    list[str],
                    connection.execute(
                        text("SELECT status FROM input_files WHERE id=ANY(:ids)"),
                        {"ids": uploaded_ids},
                    )
                    .scalars()
                    .all(),
                )
            if len(statuses) == len(uploaded_ids) and all(status == "ready_text" for status in statuses):
                break
        else:
            pytest.fail(f"uploaded coding inputs were not extracted: {statuses}")
        heartbeat(spool)
        started = launch(client, project_id, order_id)
        assert started.status_code == 201, started.text
        run_id = started.json()["run"]["id"]
        attempt_id = started.json()["attempts"][0]["id"]
        manifest = json.loads((spool / "jobs" / f"{attempt_id}.json").read_text())
        assert "Serve rural districts." in manifest["task_instructions"]
        assert "Start with outreach interviews." in manifest["task_instructions"]
        with engine.connect() as connection:
            count = connection.scalar(
                text(
                    "SELECT count(*) FROM input_file_context_uses "
                    "WHERE project_id=:project_id AND run_id=:run_id"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
        assert count == 2
        engine.dispose()


def journal_for(
    spool: Path,
    launched: dict[str, Any],
    *,
    session_id: str | None = None,
    synthetic: bool = False,
    trusted: bool = False,
) -> None:
    attempt_id = launched["attempts"][0]["id"]
    manifest = json.loads((spool / "jobs" / f"{attempt_id}.json").read_text())
    (spool / "claims" / f"{attempt_id}.claim").write_text(attempt_id)
    private = spool.parent / "journal-private"
    private.mkdir(exist_ok=True)
    request = CliRequest(
        "codex_cli",
        Path("/usr/bin/true"),
        private,
        private,
        f"{manifest['task_title']}\n\n{manifest['task_instructions']}\n",
        manifest["limits"]["wall_seconds"],
        manifest["limits"]["max_log_bytes"],
    )
    intent, digest = record_prelaunch(
        spool,
        manifest,
        request,
        "a" * 40,
        kind=(
            "trusted_local_cli_v1" if trusted else "synthetic_codex_fixture" if synthetic else "injected_cli"
        ),
        image_id="sha256:" + "a" * 64 if synthetic else None,
    )
    if session_id is not None:
        record_session(
            spool,
            intent,
            digest,
            CliResult("done", "provider_completed", session_id, 4, None, b""),
        )


def local_source(client: TestClient, project_id: str, root: Path) -> dict[str, Any]:
    repository = root / "repo"
    repository.mkdir()
    subprocess.run(  # noqa: S603 - fixed Git executable and test-owned path
        ["/usr/bin/git", "init", "-q", "-b", "main", str(repository)], check=True
    )
    (repository / "README.md").write_text("probe\n")
    subprocess.run(  # noqa: S603 - fixed Git executable and test-owned path
        ["/usr/bin/git", "-C", str(repository), "add", "README.md"], check=True
    )
    subprocess.run(  # noqa: S603 - fixed Git executable and test-owned path
        [
            "/usr/bin/git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    added = post(
        client,
        f"/api/projects/{project_id}/sources/local",
        {"root_id": "workspace", "relative_path": "repo"},
    )
    assert added.status_code == 201, added.text
    return dict(added.json())


def launch_source(client: TestClient, project_id: str, order_id: str, source_id: str) -> Any:
    return post(
        client,
        f"/api/projects/{project_id}/work-orders/{order_id}/offline-source-probe/runs",
        {"source_id": source_id},
    )


def order_status(client: TestClient, project_id: str, order_id: str) -> str:
    orders = client.get(f"/api/projects/{project_id}/work-orders").json()
    return str(next(item["status"] for item in orders if item["id"] == order_id))


def receipt(
    spool: Path,
    launched: dict[str, Any],
    *,
    valid: bool = True,
    tokens_used: int = 123,
    tamper_source: bool = False,
    invalid_probe_test: bool = False,
    with_trace: bool = False,
) -> None:
    run = launched["run"]
    attempt = launched["attempts"][0]
    attempt_id = attempt["id"]
    manifest = json.loads((spool / "jobs" / f"{attempt_id}.json").read_text())
    artifact_dir = spool / "artifacts" / attempt_id
    artifact_dir.mkdir()
    artifact_bytes = {"diff": b"diff --git a/calculator.py b/calculator.py\n", "log": b"1 passed\n"}
    artifacts = []
    for kind, data in artifact_bytes.items():
        name = "change.diff" if kind == "diff" else "after.log"
        (artifact_dir / name).write_bytes(data)
        artifacts.append(
            {
                "kind": kind,
                "path": f"artifacts/{attempt_id}/{name}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_size": len(data),
            }
        )
    if with_trace and manifest["schema_version"] == 1:
        trace_data = (
            json.dumps(
                {
                    "schema_version": 1,
                    "coverage": "partial",
                    "provider_tool_calls_captured": False,
                    "omitted_actions": 0,
                    "actions": [
                        {
                            "source": "runner_observed",
                            "kind": "command",
                            "command": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
                            "cwd": ".",
                            "started_at": "2026-09-26T10:00:00Z",
                            "finished_at": "2026-09-26T10:00:01Z",
                            "outcome": "finished",
                            "exit_code": 1,
                            "stdout": "1 failed",
                            "stderr": "",
                            "output_truncated": False,
                        }
                    ],
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        (artifact_dir / "actions.json").write_bytes(trace_data)
        artifacts.append(
            {
                "kind": "trace",
                "path": f"artifacts/{attempt_id}/actions.json",
                "sha256": hashlib.sha256(trace_data).hexdigest(),
                "byte_size": len(trace_data),
            }
        )
    payload = {
        "schema_version": manifest["schema_version"],
        "job_id": attempt_id,
        "project_id": run["project_id"],
        "run_id": run["id"],
        "attempt_id": attempt_id,
        "adapter_id": attempt["adapter_id"],
        "request_sha256": manifest["request_sha256"],
        "status": "done",
        "reason_code": "completed",
        "started_at": "2026-09-26T10:00:00Z",
        "finished_at": "2026-09-26T10:00:01Z",
        "provider_session_id": "session-123" if manifest["schema_version"] == 1 else None,
        "baseline_commit": "a" * 40,
        "branch": (
            f"fixture/repair-{attempt_id}"
            if manifest["schema_version"] == 1
            else f"onefoundry/probe-{attempt_id}"
        ),
        "commit": "b" * 40,
        "diff_sha256": artifacts[0]["sha256"],
        "artifacts": artifacts,
        "tests": [
            {"name": "fixture_test", "status": "passed", "exit_code": 0}
            if manifest["schema_version"] == 1
            else {
                "name": "git_diff_check",
                "status": "failed" if invalid_probe_test else "passed",
                "exit_code": 1 if invalid_probe_test else 0,
            }
        ],
        "tokens_used": tokens_used if manifest["schema_version"] == 1 else 0,
        "cost_usd": "0.125" if manifest["schema_version"] == 1 else "0",
    }
    if manifest["schema_version"] == 1:
        payload["fixture_id"] = "engineer_qa_v1"
    else:
        payload["target_id"] = "local_source_probe_v1"
        payload["source"] = dict(manifest["source"])
        if tamper_source:
            payload["source"]["revision"] = "f" * 40
    if not valid:
        payload["adapter_id"] = "claude_code"
    (spool / "receipts" / f"{attempt_id}.json").write_text(json.dumps(payload))


def test_launch_is_disabled_or_unavailable_without_fresh_trusted_cli_capability(spool: Path) -> None:
    with client_for(spool, enabled=False) as client:
        project_id, order_id = ready_order(client)
        root = f"/api/projects/{project_id}/work-orders/{order_id}/external-code"
        assert client.get(f"{root}/eligibility").json()["eligible"] is False
        assert launch(client, project_id, order_id).status_code == 503
    with client_for(spool, enabled=True) as client:
        for state in (
            {"age_seconds": 40},
            {"mode": "unknown"},
            {"isolated": True},
            {"auth_ready": False},
            {"available": False},
        ):
            heartbeat(spool, **state)
            result = client.get(f"{root}/eligibility")
            assert result.status_code == 200 and result.json()["eligible"] is False
            assert launch(client, project_id, order_id).status_code == 503
        (spool / "capabilities.json").write_text("{}")
        assert client.get(f"{root}/eligibility").json()["eligible"] is False
        assert launch(client, project_id, order_id).status_code == 503


def test_offline_probe_has_separate_enablement_and_readiness(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    with client_for(spool, enabled=False, local_root=root) as client:
        project_id, order_id = ready_order(client)
        source = local_source(client, project_id, root)
        path = f"/api/projects/{project_id}/work-orders/{order_id}"
        assert client.get(f"{path}/offline-source-probe/eligibility").json()["eligible"] is False
        assert launch_source(client, project_id, order_id, source["id"]).status_code == 503
        assert (
            post(
                client,
                f"{path}/external-code/runs",
                {"adapter_id": "codex_cli", "target_id": "local_source_probe_v1", "source_id": source["id"]},
            ).status_code
            == 422
        )
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        for capability in (None, {"age_seconds": 45}, {"isolated": False}):
            if capability is None:
                (spool / "probe_capabilities.json").unlink(missing_ok=True)
            else:
                probe_heartbeat(spool, **capability)
            assert client.get(f"{path}/offline-source-probe/eligibility").json()["eligible"] is False
            assert launch_source(client, project_id, order_id, source["id"]).status_code == 503
        assert order_status(client, project_id, order_id) == "ready"
        assert list((spool / "jobs").iterdir()) == []


def test_launch_receipt_readback_scope_and_no_second_attempt(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        root = f"/api/projects/{project_id}"
        assert client.get(f"{root}/work-orders/{order_id}/external-code/eligibility").json()["eligible"]
        started = launch(client, project_id, order_id)
        assert started.status_code == 201, started.text
        launched = started.json()
        assert launched["run"]["mode"] == "external_code"
        assert launched["run"]["tokens_used"] is None
        attempt = launched["attempts"][0]
        manifest = json.loads((spool / "jobs" / f"{attempt['id']}.json").read_text())
        request_hash = manifest.pop("request_sha256")
        assert (
            request_hash
            == hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        assert manifest["fixture_id"] == "engineer_qa_v1"
        assert launch(client, project_id, order_id).status_code == 409
        other_project, _ = ready_order(client)
        assert (
            client.get(
                f"/api/projects/{other_project}/runs/{launched['run']['id']}/external-code"
            ).status_code
            == 404
        )
        journal_for(spool, launched, session_id="session-123", synthetic=True)
        receipt(spool, launched, with_trace=True)
        saved = post(client, f"{root}/runs/{launched['run']['id']}/external-code/reconcile")
        assert saved.status_code == 200, saved.text
        detail = saved.json()
        assert detail["run"]["status"] == "done", detail
        assert detail["run"]["tokens_used"] == 123
        assert detail["attempts"][0]["provider_session_id"] == "session-123"
        assert detail["attempts"][0]["diff_sha256"] is not None
        assert detail["attempts"][0]["tests"][0]["status"] == "passed"
        artifact = detail["attempts"][0]["artifacts"][0]
        preview = client.get(f"{root}/runs/{launched['run']['id']}/external-code/artifacts/{artifact['id']}")
        assert preview.status_code == 200 and preview.json()["sha256"] == artifact["sha256"]
        trace = next(item for item in detail["attempts"][0]["artifacts"] if item["kind"] == "trace")
        trace_preview = client.get(
            f"{root}/runs/{launched['run']['id']}/external-code/artifacts/{trace['id']}"
        )
        assert trace_preview.status_code == 200, trace_preview.text
        assert json.loads(trace_preview.json()["text"])["actions"][0]["exit_code"] == 1
        assert (
            client.get(
                f"/api/projects/{other_project}/runs/{launched['run']['id']}/external-code/artifacts/{artifact['id']}"
            ).status_code
            == 404
        )
        assert post(client, f"{root}/runs/{launched['run']['id']}/external-code/reconcile").json() == detail
        engine = create_engine(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
            ).database_url
        )
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("UPDATE external_code_attempts SET adapter_id='claude_code' WHERE id=:id"),
                {"id": attempt["id"]},
            )
        engine.dispose()


def test_local_source_tampered_receipt_is_unknown(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    probe_heartbeat(spool)
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        project_id, order_id = ready_order(client)
        source = local_source(client, project_id, root)
        started = launch_source(client, project_id, order_id, source["id"])
        assert started.status_code == 201, started.text
        receipt(spool, started.json(), tamper_source=True)
        result = post(
            client,
            f"/api/projects/{project_id}/runs/{started.json()['run']['id']}/external-code/reconcile",
        )
        assert result.status_code == 200, result.text
        assert result.json()["run"]["status"] == "unknown"
        assert result.json()["attempts"][0]["reason_code"] == "receipt_invalid"


def test_local_source_failed_diff_check_cannot_finish_done(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    probe_heartbeat(spool)
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        project_id, order_id = ready_order(client)
        source = local_source(client, project_id, root)
        started = launch_source(client, project_id, order_id, source["id"])
        assert started.status_code == 201, started.text
        receipt(spool, started.json(), invalid_probe_test=True)
        result = post(
            client,
            f"/api/projects/{project_id}/runs/{started.json()['run']['id']}/external-code/reconcile",
        )
        assert result.status_code == 200, result.text
        assert result.json()["run"]["status"] == "unknown"
        assert result.json()["attempts"][0]["reason_code"] == "receipt_invalid"


def test_local_source_attempt_binds_exact_selection_and_digest(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    probe_heartbeat(spool)
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        project_id, order_id = ready_order(client)
        source = local_source(client, project_id, root)
        other_project, other_order = ready_order(client)
        assert launch_source(client, other_project, other_order, source["id"]).status_code == 404
        assert order_status(client, other_project, other_order) == "ready"
        assert list((spool / "jobs").iterdir()) == []
        started = launch_source(client, project_id, order_id, source["id"])
        assert started.status_code == 201, started.text
        attempt = started.json()["attempts"][0]
        assert attempt["adapter_id"] == "offline_probe"
        assert attempt["target_id"] == attempt["fixture_id"] == "local_source_probe_v1"
        assert attempt["source_id"] == source["id"]
        assert attempt["source_root_id"] == "workspace"
        assert attempt["source_relative_path"] == "repo"
        assert attempt["source_branch"] == source["branch"]
        assert attempt["source_revision"] == source["revision"]
        manifest = json.loads((spool / "jobs" / f"{attempt['id']}.json").read_text())
        digest = manifest.pop("request_sha256")
        assert manifest["schema_version"] == 2
        assert manifest["adapter_id"] == "offline_probe"
        assert manifest["target_id"] == "local_source_probe_v1"
        assert manifest["source"] == {
            "source_id": source["id"],
            "root_id": "workspace",
            "relative_path": "repo",
            "branch": source["branch"],
            "revision": source["revision"],
        }
        assert digest == attempt["request_sha256"]
        assert (
            digest
            == hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        engine = create_engine(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
            ).database_url
        )
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("UPDATE external_code_attempts SET source_revision=:revision WHERE id=:id"),
                {"id": attempt["id"], "revision": "f" * 40},
            )
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("UPDATE project_sources SET id=:new_id WHERE project_id=:project_id AND id=:id"),
                {"new_id": uuid4(), "project_id": project_id, "id": source["id"]},
            )
        engine.dispose()
        receipt(spool, started.json())
        saved = post(
            client,
            f"/api/projects/{project_id}/runs/{started.json()['run']['id']}/external-code/reconcile",
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["run"]["status"] == "done"
        assert saved.json()["attempts"][0]["baseline_commit"] != source["revision"]


def test_local_source_stale_or_deleted_fails_without_dispatch(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    probe_heartbeat(spool)
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        project_id, order_id = ready_order(client)
        source = local_source(client, project_id, root)
        repository = root / "repo"
        (repository / "README.md").write_text("changed\n")
        subprocess.run(  # noqa: S603 - fixed Git executable and test-owned path
            ["/usr/bin/git", "-C", str(repository), "add", "README.md"], check=True
        )
        subprocess.run(  # noqa: S603 - fixed Git executable and test-owned path
            [
                "/usr/bin/git",
                "-C",
                str(repository),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "changed",
            ],
            check=True,
        )
        stale = launch_source(client, project_id, order_id, source["id"])
        assert stale.status_code == 409 and "changed" in stale.text
        assert order_status(client, project_id, order_id) == "ready"
        assert list((spool / "jobs").iterdir()) == []
        repository.rename(root / "removed")
        missing = launch_source(client, project_id, order_id, source["id"])
        assert missing.status_code == 409 and "unavailable" in missing.text
        assert order_status(client, project_id, order_id) == "ready"
        assert list((spool / "jobs").iterdir()) == []
        assert (
            client.delete(
                f"/api/projects/{project_id}/sources/{source['id']}",
                headers={"origin": "http://localhost:8765"},
            ).status_code
            == 204
        )
        assert launch_source(client, project_id, order_id, source["id"]).status_code == 404
        assert list((spool / "jobs").iterdir()) == []


def test_local_source_launch_serializes_with_other_launch_and_deletion(spool: Path, tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    probe_heartbeat(spool)
    with client_for(spool, enabled=False, local_root=root, offline_enabled=True) as client:
        project_id, order_id = ready_order(client)
        source = local_source(client, project_id, root)
        entered, release = Event(), Event()
        from onefoundry.source_store import verify_local as actual_verify

        def paused_verify(settings: Settings, relative_path: str) -> tuple[str, str]:
            entered.set()
            assert release.wait(5)
            return actual_verify(settings, relative_path)

        with patch("onefoundry.execution_gateway.verify_local", side_effect=paused_verify):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(launch_source, client, project_id, order_id, source["id"])
                assert entered.wait(5)
                second = executor.submit(launch_source, client, project_id, order_id, source["id"])
                engine = create_engine(
                    Settings(
                        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                        db_password=os.environ["APP_DB_PASSWORD"],
                    ).database_url
                )
                with engine.begin() as connection, pytest.raises(DBAPIError):
                    connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    connection.execute(
                        text("DELETE FROM project_sources WHERE project_id=:project_id AND id=:id"),
                        {"project_id": project_id, "id": source["id"]},
                    )
                engine.dispose()
                release.set()
                assert first.result(timeout=10).status_code == 201
                assert second.result(timeout=10).status_code == 409
                assert (
                    client.delete(
                        f"/api/projects/{project_id}/sources/{source['id']}",
                        headers={"origin": "http://localhost:8765"},
                    ).status_code
                    == 204
                )
        assert len(list((spool / "jobs").glob("*.json"))) == 1


def test_bad_receipt_becomes_unknown_without_retry(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        receipt(spool, launched, valid=False)
        run_id = launched["run"]["id"]
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200
        assert result.json()["attempts"][0]["status"] == "unknown"
        assert result.json()["run"]["status"] == "unknown"
        assert len(list((spool / "jobs").glob("*.json"))) == 1
        assert (
            post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile").json()
            == result.json()
        )


def test_oversized_token_receipt_becomes_unknown_once(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        receipt(spool, launched, tokens_used=2_147_483_648)
        run_id = launched["run"]["id"]
        path = f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile"
        result = post(client, path)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["run"]["status"] == "unknown"
        assert saved["attempts"][0]["status"] == "unknown"
        assert saved["attempts"][0]["reason_code"] == "receipt_invalid"
        assert post(client, path).json() == saved
        events = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()["events"]
        assert sum(item["event_type"] == "external_code_unknown" for item in events) == 1


def test_claimed_receipt_missing_is_unknown(spool: Path) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        attempt_id = launched["attempts"][0]["id"]
        claim = spool / "claims" / f"{attempt_id}.claim"
        claim.write_text(attempt_id)
        old = datetime.now(UTC) - timedelta(seconds=180)
        os.utime(claim, (old.timestamp(), old.timestamp()))
        run_id = launched["run"]["id"]
        outcome = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert outcome.status_code == 200
        assert outcome.json()["attempts"][0]["reason_code"] == "claimed_receipt_missing"
        assert len(list((spool / "jobs").glob("*.json"))) == 1


@pytest.mark.parametrize("observed_session", [None, "observed-session"])
def test_claimed_launch_without_receipt_reconciles_unknown_without_relaunch(
    spool: Path, observed_session: str | None, isolated_model_secrets: None
) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        attempt_id = launched["attempts"][0]["id"]
        run_id = launched["run"]["id"]
        journal_for(spool, launched, session_id=observed_session)
        claim = spool / "claims" / f"{attempt_id}.claim"
        old = datetime.now(UTC) - timedelta(seconds=180)
        os.utime(claim, (old.timestamp(), old.timestamp()))
        path = f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile"
        result = post(client, path)
        assert result.status_code == 200, result.text
        detail = result.json()
        assert detail["run"]["status"] == "unknown"
        assert detail["attempts"][0]["reason_code"] == "claimed_receipt_missing"
        assert detail["attempts"][0]["provider_session_id"] == observed_session
        events = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()["events"]
        unknown = [item for item in events if item["event_type"] == "external_code_unknown"]
        assert len(unknown) == 1
        assert unknown[0]["payload"]["session_observed"] is (observed_session is not None)
        assert post(client, path).json() == detail
        assert len(list((spool / "jobs").glob("*.json"))) == 1
        assert not (spool / "receipts" / f"{attempt_id}.json").exists()


def test_malformed_launch_sidecar_cannot_reconcile_success(spool: Path, isolated_model_secrets: None) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        attempt_id = launched["attempts"][0]["id"]
        run_id = launched["run"]["id"]
        receipt(spool, launched)
        (spool / "claims" / f"{attempt_id}.launch.json").write_text('{"schema_version":1}')
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200, result.text
        detail = result.json()
        assert detail["run"]["status"] == "unknown"
        assert detail["attempts"][0]["reason_code"] == "launch_record_invalid"
        assert detail["attempts"][0]["artifacts"] == []


def test_receipt_conflicting_with_observed_session_is_unknown(
    spool: Path, isolated_model_secrets: None
) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        run_id = launched["run"]["id"]
        journal_for(spool, launched, session_id="observed-session")
        receipt(spool, launched)
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200, result.text
        detail = result.json()
        assert detail["run"]["status"] == "unknown"
        assert detail["attempts"][0]["reason_code"] == "receipt_invalid"
        assert detail["attempts"][0]["provider_session_id"] == "observed-session"


def test_injected_cli_receipt_cannot_reconcile_done(spool: Path, isolated_model_secrets: None) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        run_id = launched["run"]["id"]
        journal_for(spool, launched, session_id="session-123")
        receipt(spool, launched)
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200, result.text
        detail = result.json()
        assert detail["run"]["status"] == "unknown"
        assert detail["attempts"][0]["reason_code"] == "receipt_invalid"


def test_trusted_local_cli_receipt_reconciles_done(spool: Path, isolated_model_secrets: None) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        run_id = launched["run"]["id"]
        journal_for(spool, launched, session_id="session-123", trusted=True)
        receipt(spool, launched, with_trace=True)
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200, result.text
        assert result.json()["run"]["status"] == "done"
        assert result.json()["attempts"][0]["provider_session_id"] == "session-123"


def test_receipt_without_launch_cannot_reconcile_done(spool: Path, isolated_model_secrets: None) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        run_id = launched["run"]["id"]
        receipt(spool, launched)
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200, result.text
        detail = result.json()
        assert detail["run"]["status"] == "unknown"
        assert detail["attempts"][0]["reason_code"] == "receipt_invalid"


@pytest.mark.parametrize("tamper", ["digest", "symlink"])
def test_tampered_launch_evidence_cannot_reconcile_success(
    spool: Path, isolated_model_secrets: None, tamper: str
) -> None:
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        launched = launch(client, project_id, order_id).json()
        attempt_id = launched["attempts"][0]["id"]
        run_id = launched["run"]["id"]
        journal_for(spool, launched, session_id="session-123")
        receipt(spool, launched)
        if tamper == "digest":
            path = spool / "claims" / f"{attempt_id}.session.json"
            payload = json.loads(path.read_text())
            payload["launch_sha256"] = "f" * 64
            path.write_text(json.dumps(payload))
        else:
            path = spool / "claims" / f"{attempt_id}.launch.json"
            target = spool / "claims" / "moved-launch.json"
            path.rename(target)
            path.symlink_to(target)
        result = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert result.status_code == 200, result.text
        detail = result.json()
        assert detail["run"]["status"] == "unknown"
        assert detail["attempts"][0]["reason_code"] == "launch_record_invalid"
