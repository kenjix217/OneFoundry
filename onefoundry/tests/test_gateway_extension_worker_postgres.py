"""Gateway reconciliation of isolated-worker callbacks, waits and exact receipts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.gateway_extension_calls import settle
from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.gateway_extension_runtime import execute_saved, invoke
from onefoundry.gateway_extension_spool import prepare_spool, publish_json, read_json
from onefoundry.gateway_extension_worker_expiry import expire_queued
from onefoundry.gateway_extension_worker_runtime import reconcile_once
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.tool_gateway import ToolGateway
from tests.test_gateway_extension_remote_postgres import ORIGIN, _Handler, _post, _ready_extension
from tests.test_gateway_extension_remote_postgres import remote as remote
from tests.test_gateway_extension_remote_postgres import setup as setup


@pytest.fixture(autouse=True)
def manual_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """These crash-point tests explicitly advance each Gateway reconciliation."""
    from onefoundry.gateway_extension_worker_runtime import ExtensionWorkerRuntime

    async def start(_runtime: ExtensionWorkerRuntime) -> None:
        return None

    monkeypatch.setattr(ExtensionWorkerRuntime, "start", start)


def _python_run(
    client: TestClient, gateway: ToolGateway, remote: str, tmp_path: Path
) -> tuple[UUID, UUID, UUID, str, Path]:
    project_id, _, profile_id, _ = _ready_extension(client, gateway, remote)
    with gateway.engine.connect() as connection:
        source: dict[str, Any] = connection.execute(
            text("SELECT manifest FROM extension_packages WHERE project_id=:p"), {"p": project_id}
        ).scalar_one()
        connection_id: UUID = connection.execute(
            text("SELECT id FROM extension_connections WHERE project_id=:p"), {"p": project_id}
        ).scalar_one()
    source["id"] = "python-callback"
    source["tools"].append(
        {
            "id": "compute",
            "description": "Compute using a declared source",
            "effect": "pure",
            "runtime": "python",
            "entrypoint": "compute:run",
            "required_connections": ["api"],
            "parameters": {"type": "object"},
            "returns": {"type": "object"},
        }
    )
    base = f"/api/projects/{project_id}"
    package = _post(
        client,
        f"{base}/extensions/packages",
        {
            "manifest": source,
            "files": {
                "src/compute.py": "async def run(context):\n"
                "    return dict(await context.connections.invoke(\n"
                "        slot='api', operation='lookup', arguments={'term': 'ok'}))\n"
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
    bound = client.put(
        f"{base}/extensions/packages/{package['id']}/bindings/api",
        json={"connection_id": str(connection_id), "expected_version": 0},
        headers=ORIGIN,
    )
    assert bound.status_code == 200, bound.text
    tool_id = canonical_tool_id("python-callback", "compute")
    target_id = canonical_tool_id("python-callback", "lookup")
    response = client.patch(
        f"{base}/profiles/{profile_id}", json={"allowed_tools": [tool_id, target_id]}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    settings = gateway.live_text_settings
    assert settings is not None
    settings.runner_spool_root = str(tmp_path / "spool")
    settings.extension_worker_image = "sha256:" + "a" * 64
    order = _post(
        client,
        f"{base}/work-orders",
        {"title": "Compute fixture", "instructions": "Compute", "profile_id": str(profile_id)},
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready", {})
    run = gateway.start_synthetic_text(project_id, UUID(str(order["id"])))
    root = prepare_spool(settings)
    publish_json(
        root / "heartbeat.json",
        {
            "schema_version": 1,
            "image_id": settings.extension_worker_image,
            "updated_at": datetime.now(UTC).isoformat(),
            "supervisor_id": str(project_id),
            "memory_mb": settings.extension_worker_memory_mb,
            "processes": settings.extension_worker_processes,
            "cpus": settings.extension_worker_cpus,
            "max_timeout_seconds": settings.extension_timeout_seconds,
            "max_output_bytes": settings.extension_worker_max_output_bytes,
        },
    )
    return project_id, UUID(str(run["run_id"])), profile_id, tool_id, root


def _callback(
    root: Path, receipt: dict[str, Any], job: dict[str, Any], *, term: str = "ok"
) -> dict[str, Any]:
    request = {
        "schema_version": 1,
        "invocation_id": receipt["invocation_id"],
        "job_sha256": job["job_sha256"],
        "sequence": 1,
        "slot": "api",
        "operation": "lookup",
        "arguments": {"term": term},
    }
    publish_json(root / "callbacks" / "requests" / f"{receipt['invocation_id']}-1.json", request)
    return request


@pytest.mark.parametrize(
    "crash",
    [
        None,
        "before_checkpoint",
        "before_publish",
        "after_publish",
        "cancel_after_checkpoint",
        "child_done_early",
        "nested_unknown",
        "expired_after_checkpoint",
        "supervisor_down_after_checkpoint",
        "revoked_after_checkpoint",
    ],
)
def test_callback_wait_resume_and_exact_cached_effect(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash: str | None,
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-1", tool_id, {}, 1)
    assert parent["status"] == "running"
    identity = parent["invocation_id"]
    job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    term = "fail" if crash == "nested_unknown" else "ok"
    _callback(root, parent, job, term=term)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    assert response["status"] == "approval_required"
    assert _Handler.calls == []
    if crash == "child_done_early":
        _post(
            client,
            f"/api/projects/{project_id}/approvals/{response['approval_id']}/decision",
            {"decision": "approved"},
        )
        assert execute_saved(gateway, project_id, UUID(response["waiting_invocation_id"]))["status"] == "done"
    publish_json(
        root / "receipts" / f"{identity}.json",
        {
            **{key: job[key] for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")},
            "status": "approval_required",
            "approval_id": response["approval_id"],
            "waiting_invocation_id": response["waiting_invocation_id"],
        },
    )
    reconcile_once(gateway)
    if crash != "child_done_early":
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM extension_invocations WHERE id=:id"), {"id": identity}
                )
                == "waiting"
            )
        _post(
            client,
            f"/api/projects/{project_id}/approvals/{response['approval_id']}/decision",
            {"decision": "approved"},
        )
    if crash == "cancel_after_checkpoint":
        from onefoundry import gateway_extension_worker_receipts as receipts

        complete = receipts._complete_resume

        def cancel_before_resume(*args: Any, **kwargs: Any) -> Any:
            _post(client, f"/api/projects/{project_id}/extensions/invocations/{identity}/cancel", {})
            return complete(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(receipts, "_complete_resume", cancel_before_resume)
            reconcile_once(gateway)
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM extension_invocations WHERE id=:id"), {"id": identity}
                )
                == "cancelled"
            )
        assert (root / "cancellations" / f"{identity}.json").exists()
        assert (
            read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)["job_sha256"] == job["job_sha256"]
        )
        assert _Handler.calls == []
        return
    if crash and crash not in {"child_done_early", "nested_unknown"}:
        from onefoundry import gateway_extension_worker_receipts as receipts

        function_name = "_copy_wait_evidence" if crash == "before_checkpoint" else "_worker_job"
        original = getattr(receipts, function_name)

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            if crash not in {
                "before_publish",
                "expired_after_checkpoint",
                "supervisor_down_after_checkpoint",
                "revoked_after_checkpoint",
            }:
                original(*args, **kwargs)
            raise RuntimeError("injected process interruption")

        with monkeypatch.context() as patch:
            patch.setattr(receipts, function_name, interrupted)
            with pytest.raises(RuntimeError, match="injected process interruption"):
                reconcile_once(gateway)
        if crash in {
            "expired_after_checkpoint",
            "supervisor_down_after_checkpoint",
            "revoked_after_checkpoint",
        }:
            if crash == "supervisor_down_after_checkpoint":
                (root / "heartbeat.json").unlink()
            if crash == "revoked_after_checkpoint":
                changed = client.patch(
                    f"/api/projects/{project_id}/profiles/{profile_id}",
                    json={"allowed_tools": []},
                    headers=ORIGIN,
                )
                assert changed.status_code == 200
                reconcile_once(gateway)
            else:
                from onefoundry import gateway_extension_worker_runtime as runtime

                with monkeypatch.context() as patch:
                    patch.setattr(
                        runtime,
                        "expire_queued",
                        lambda gateway, root: expire_queued(
                            gateway, root, now=datetime.now(UTC) + timedelta(seconds=601)
                        ),
                    )
                    reconcile_once(gateway)
            with gateway.engine.connect() as connection:
                status = connection.scalar(
                    text("SELECT status FROM extension_invocations WHERE id=:id"), {"id": identity}
                )
            assert status == ("unknown" if crash == "revoked_after_checkpoint" else "cancelled")
            assert (root / "cancellations" / f"{identity}.json").exists()
            assert not (root / "jobs" / f"{identity}.json").exists()
            assert _Handler.calls == []
            return
    reconcile_once(gateway)
    new_job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    assert new_job["job_sha256"] != job["job_sha256"]
    assert (root / "history" / identity / job["job_sha256"] / "receipts.json").exists()
    _callback(root, parent, new_job, term=term)
    reconcile_once(gateway)
    done_callback = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    if crash == "nested_unknown":
        assert done_callback["status"] == "unknown"
        publish_json(
            root / "receipts" / f"{identity}.json",
            {
                **{
                    key: new_job[key]
                    for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")
                },
                "status": "unknown",
                "waiting_invocation_id": done_callback["waiting_invocation_id"],
            },
        )
        reconcile_once(gateway)
        with gateway.engine.connect() as connection:
            saved_unknown = (
                connection.execute(
                    text("SELECT status,waiting_invocation_id FROM extension_invocations WHERE id=:id"),
                    {"id": identity},
                )
                .mappings()
                .one()
            )
        assert saved_unknown["status"] == "unknown"
        assert str(saved_unknown["waiting_invocation_id"]) == done_callback["waiting_invocation_id"]
        assert len(_Handler.calls) == 1
        return
    assert done_callback["status"] == "done"
    assert len(_Handler.calls) == 1
    publish_json(
        root / "receipts" / f"{identity}.json",
        {
            **{
                key: new_job[key]
                for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")
            },
            "status": "done",
            "result": done_callback["result"],
        },
    )
    reconcile_once(gateway)
    result = invoke(gateway, project_id, run_id, profile_id, "python-1", tool_id, {}, 1)
    assert result["status"] == "done"
    assert result["result"] == {"value": "ok"}
    reconcile_once(gateway)
    assert len(_Handler.calls) == 1


def test_cancelled_parent_cannot_dispatch_a_durable_callback(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-cancel", tool_id, {}, 1)
    job = read_json(root / "jobs" / f"{parent['invocation_id']}.json", 7 * 1024 * 1024)
    _post(client, f"/api/projects/{project_id}/extensions/invocations/{parent['invocation_id']}/cancel", {})
    _callback(root, parent, job)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{parent['invocation_id']}-1.json", 65536)
    assert response["status"] == "denied"
    assert _Handler.calls == []


def test_cancel_between_callback_receipt_and_wait_terminalizes_both(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onefoundry import gateway_extension_worker_receipts as receipts

    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-cancel-race", tool_id, {}, 1)
    identity = parent["invocation_id"]
    job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    _callback(root, parent, job)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    publish_json(
        root / "receipts" / f"{identity}.json",
        {
            **{key: job[key] for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")},
            "status": "approval_required",
            "approval_id": response["approval_id"],
            "waiting_invocation_id": response["waiting_invocation_id"],
        },
    )
    original = receipts._record_wait

    def cancel_before_record(*args: Any, **kwargs: Any) -> None:
        _post(client, f"/api/projects/{project_id}/extensions/invocations/{identity}/cancel", {})
        original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(receipts, "_record_wait", cancel_before_record)
        reconcile_once(gateway)
    reconcile_once(gateway)
    with gateway.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id,status FROM extension_invocations WHERE id IN (:parent,:child)"),
            {"parent": UUID(identity), "child": UUID(response["waiting_invocation_id"])},
        ).all()
    assert {row.id: row.status for row in rows} == {
        UUID(identity): "cancelled",
        UUID(response["waiting_invocation_id"]): "cancelled",
    }
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{response['approval_id']}/decision",
        {"decision": "approved"},
    )
    with pytest.raises(ToolDenied):
        execute_saved(gateway, project_id, UUID(response["waiting_invocation_id"]))
    assert _Handler.calls == []


@pytest.mark.parametrize("reject_before_receipt", [False, True])
def test_rejected_callback_approval_terminalizes_parent_without_heartbeat(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    reject_before_receipt: bool,
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-rejected", tool_id, {}, 1)
    identity = parent["invocation_id"]
    job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    _callback(root, parent, job)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    if not reject_before_receipt:
        publish_json(
            root / "receipts" / f"{identity}.json",
            {
                **{
                    key: job[key]
                    for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")
                },
                "status": "approval_required",
                "approval_id": response["approval_id"],
                "waiting_invocation_id": response["waiting_invocation_id"],
            },
        )
        reconcile_once(gateway)
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{response['approval_id']}/decision",
        {"decision": "rejected"},
    )
    with gateway.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id,status FROM extension_invocations WHERE id IN (:parent,:child)"),
            {"parent": UUID(identity), "child": UUID(response["waiting_invocation_id"])},
        ).all()
    assert {row.id: row.status for row in rows} == {
        UUID(identity): "failed",
        UUID(response["waiting_invocation_id"]): "failed",
    }
    if reject_before_receipt:
        publish_json(
            root / "receipts" / f"{identity}.json",
            {
                **{
                    key: job[key]
                    for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")
                },
                "status": "approval_required",
                "approval_id": response["approval_id"],
                "waiting_invocation_id": response["waiting_invocation_id"],
            },
        )
    reconcile_once(gateway)
    with pytest.raises(ToolDenied):
        execute_saved(gateway, project_id, UUID(response["waiting_invocation_id"]))
    assert _Handler.calls == []


@pytest.mark.parametrize("child_status", ["failed", "unknown"])
def test_terminal_child_settles_waiting_parent_without_replaying(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    child_status: str,
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-terminal-child", tool_id, {}, 1)
    identity = parent["invocation_id"]
    job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    _callback(root, parent, job)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    child_id = UUID(response["waiting_invocation_id"])
    publish_json(
        root / "receipts" / f"{identity}.json",
        {
            **{key: job[key] for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")},
            "status": "approval_required",
            "approval_id": response["approval_id"],
            "waiting_invocation_id": str(child_id),
        },
    )
    reconcile_once(gateway)
    settle(
        gateway,
        project_id,
        child_id,
        child_status,
        reason="fixture_terminal_child",
        expected_status="approval_required",
    )
    (root / "heartbeat.json").unlink()
    reconcile_once(gateway)
    reconcile_once(gateway)
    with gateway.engine.connect() as connection:
        row = connection.execute(
            text("SELECT status,waiting_invocation_id FROM extension_invocations WHERE id=:id"),
            {"id": UUID(identity)},
        ).one()
    assert row.status == child_status
    assert row.waiting_invocation_id == child_id
    assert read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)["job_sha256"] == job["job_sha256"]
    assert _Handler.calls == []


def test_cancellation_wins_over_callback_rejection(
    setup: tuple[TestClient, ToolGateway], remote: str, tmp_path: Path
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-cancel-reject", tool_id, {}, 1)
    identity = parent["invocation_id"]
    job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    _callback(root, parent, job)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    _post(client, f"/api/projects/{project_id}/extensions/invocations/{identity}/cancel", {})
    _post(
        client,
        f"/api/projects/{project_id}/approvals/{response['approval_id']}/decision",
        {"decision": "rejected"},
    )
    reconcile_once(gateway)
    with gateway.engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id,status FROM extension_invocations WHERE id IN (:parent,:child)"),
            {"parent": UUID(identity), "child": UUID(response["waiting_invocation_id"])},
        ).all()
    assert {row.id: row.status for row in rows} == {
        UUID(identity): "cancelled",
        UUID(response["waiting_invocation_id"]): "cancelled",
    }
    assert _Handler.calls == []


@pytest.mark.parametrize("already_waiting", [False, True])
def test_reconcile_committed_rejection_after_hook_interruption(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    already_waiting: bool,
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-rejection-recovery", tool_id, {}, 1)
    identity = parent["invocation_id"]
    job = read_json(root / "jobs" / f"{identity}.json", 7 * 1024 * 1024)
    _callback(root, parent, job)
    reconcile_once(gateway)
    response = read_json(root / "callbacks" / "responses" / f"{identity}-1.json", 65536)
    child_id = UUID(response["waiting_invocation_id"])
    publish_json(
        root / "receipts" / f"{identity}.json",
        {
            **{key: job[key] for key in ("schema_version", "invocation_id", "job_sha256", "package_sha256")},
            "status": "approval_required",
            "approval_id": response["approval_id"],
            "waiting_invocation_id": str(child_id),
        },
    )
    if already_waiting:
        reconcile_once(gateway)
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE approval_requests SET status='rejected',decided_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": UUID(response["approval_id"])},
        )
    (root / "heartbeat.json").unlink()
    reconcile_once(gateway)
    reconcile_once(gateway)
    with gateway.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id,status,waiting_invocation_id FROM extension_invocations "
                "WHERE id IN (:parent,:child)"
            ),
            {"parent": UUID(identity), "child": child_id},
        ).all()
    states = {row.id: row for row in rows}
    assert states[UUID(identity)].status == "failed"
    assert states[UUID(identity)].waiting_invocation_id == child_id
    assert states[child_id].status == "failed"
    with pytest.raises(ToolDenied):
        execute_saved(gateway, project_id, child_id)
    assert _Handler.calls == []


@pytest.mark.parametrize("claimed", [False, True])
def test_expired_queue_never_replays_and_distinguishes_possible_launch(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    claimed: bool,
) -> None:
    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    parent = invoke(gateway, project_id, run_id, profile_id, "python-expiry", tool_id, {}, 1)
    identity = parent["invocation_id"]
    if claimed:
        publish_json(root / "claims" / f"{identity}.json", {"claimed": True})
    expire_queued(gateway, root, now=datetime.now(UTC) + timedelta(seconds=601))
    with gateway.engine.connect() as connection:
        status = connection.scalar(
            text("SELECT status FROM extension_invocations WHERE id=:id"), {"id": identity}
        )
    assert status == ("unknown" if claimed else "cancelled")
    assert (root / "cancellations" / f"{identity}.json").exists()
    assert _Handler.calls == []


def test_claim_without_published_job_expires_without_launch(
    setup: tuple[TestClient, ToolGateway],
    remote: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onefoundry import gateway_extension_runtime as runtime

    class Interrupted(BaseException):
        pass

    def interrupt(*_args: object) -> None:
        raise Interrupted

    client, gateway = setup
    project_id, run_id, profile_id, tool_id, root = _python_run(client, gateway, remote, tmp_path)
    monkeypatch.setattr(runtime, "_worker_job", interrupt)
    with pytest.raises(Interrupted):
        invoke(gateway, project_id, run_id, profile_id, "unpublished", tool_id, {}, 1)
    with gateway.engine.connect() as connection:
        row = connection.execute(
            text("SELECT id,status,started_at FROM extension_invocations WHERE run_id=:run"),
            {"run": run_id},
        ).one()
    assert row.status == "running" and row.started_at is not None
    assert not (root / "jobs" / f"{row.id}.json").exists()
    expire_queued(gateway, root, now=datetime.now(UTC) + timedelta(seconds=601))
    expire_queued(gateway, root, now=datetime.now(UTC) + timedelta(seconds=602))
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT status FROM extension_invocations WHERE id=:id"), {"id": row.id})
            == "cancelled"
        )
    assert (root / "cancellations" / f"{row.id}.json").exists()
    with pytest.raises(ToolDenied):
        invoke(gateway, project_id, run_id, profile_id, "unpublished", tool_id, {}, 1)
    assert _Handler.calls == []
