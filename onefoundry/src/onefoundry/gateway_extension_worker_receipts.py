"""Reconcile one-shot host receipts and known approval waits without replaying uncertainty."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_extension_calls import lock_invocation_run, settle
from onefoundry.gateway_extension_registry import ExtensionTool
from onefoundry.gateway_extension_runtime import _checked_result, _worker_job
from onefoundry.gateway_extension_spool import private_directory, read_json
from onefoundry.gateway_extension_wait_terminal import reject_callback_approval
from onefoundry.gateway_extension_worker_context import admitted_parent
from onefoundry.gateway_extension_worker_readiness import worker_status
from onefoundry.role_tool_admission import ToolDenied, ToolOutcomeUnknown, ToolOutputRejected

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _record_wait(
    gateway: ToolGateway, root: Path, saved: dict[str, Any], job: dict[str, Any], receipt: dict[str, Any]
) -> None:
    nested_id, approval_id = UUID(receipt["waiting_invocation_id"]), UUID(receipt["approval_id"])
    proof = False
    for response_path in (root / "callbacks" / "responses").glob(f"{saved['id']}-*.json"):
        response = read_json(response_path, 65536)
        if (
            response.get("job_sha256") == job["job_sha256"]
            and response.get("status") == "approval_required"
            and response.get("waiting_invocation_id") == str(nested_id)
            and response.get("approval_id") == str(approval_id)
        ):
            proof = True
    if not proof:
        raise ToolDenied("worker approval wait has no gateway callback receipt")
    with gateway.engine.begin() as connection:
        lock_invocation_run(connection, saved["project_id"], saved["id"])
        parent = (
            connection.execute(
                text(
                    "SELECT status,reason FROM extension_invocations "
                    "WHERE project_id=:p AND id=:id FOR UPDATE"
                ),
                {"p": saved["project_id"], "id": saved["id"]},
            )
            .mappings()
            .one()
        )
        if parent["status"] != "running":
            return
        nested = (
            connection.execute(
                text(
                    "SELECT run_id,profile_id,approval_id,status FROM extension_invocations "
                    "WHERE project_id=:p AND id=:id FOR UPDATE"
                ),
                {"p": saved["project_id"], "id": nested_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            nested is None
            or nested["run_id"] != saved["run_id"]
            or nested["profile_id"] != saved["profile_id"]
            or nested["approval_id"] != approval_id
        ):
            raise ToolDenied("worker approval wait differs from the admitted nested invocation")
        cancellation = root / "cancellations" / f"{saved['id']}.json"
        if parent["reason"] == "cancellation_requested" or cancellation.exists():
            connection.execute(
                text(
                    "UPDATE extension_invocations SET status='cancelled',reason='operator_cancelled',"
                    "waiting_invocation_id=:nested,finished_at=clock_timestamp() "
                    "WHERE project_id=:p AND id=:id AND status='running'"
                ),
                {"p": saved["project_id"], "id": saved["id"], "nested": nested_id},
            )
            connection.execute(
                text(
                    "UPDATE extension_invocations SET status='cancelled',reason='parent_cancelled',"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id "
                    "AND status='approval_required'"
                ),
                {"p": saved["project_id"], "id": nested_id},
            )
            order_id = connection.scalar(
                text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:run"),
                {"p": saved["project_id"], "run": saved["run_id"]},
            )
            gateway._event(
                connection,
                saved["project_id"],
                order_id,
                saved["run_id"],
                "extension_invocation_finished",
                {"invocation_id": str(saved["id"]), "status": "cancelled", "reason": "operator_cancelled"},
                actor_id=saved["actor_id"],
            )
            return
        if nested["status"] not in {"approval_required", "done"}:
            raise ToolDenied("worker approval wait differs from the admitted nested invocation")
        updated = connection.execute(
            text(
                "UPDATE extension_invocations SET status='waiting',waiting_invocation_id=:nested,"
                "reason='connection_approval_required' "
                "WHERE project_id=:p AND id=:id AND status='running' RETURNING run_id"
            ),
            {"p": saved["project_id"], "id": saved["id"], "nested": nested_id},
        ).one_or_none()
        if updated is None:
            raise ToolDenied("worker invocation changed during reconciliation")
        order_id = connection.scalar(
            text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:run"),
            {"p": saved["project_id"], "run": saved["run_id"]},
        )
        gateway._event(
            connection,
            saved["project_id"],
            order_id,
            saved["run_id"],
            "extension_invocation_waiting",
            {
                "invocation_id": str(saved["id"]),
                "approval_id": str(approval_id),
                "waiting_invocation_id": str(nested_id),
            },
            actor_id=saved["actor_id"],
        )


def process_receipt(gateway: ToolGateway, root: Path, path: Path) -> None:
    invocation_id = UUID(path.stem)
    with gateway.engine.connect() as connection:
        current = (
            connection.execute(
                text("SELECT project_id,status,worker_resume_from FROM extension_invocations WHERE id=:id"),
                {"id": invocation_id},
            )
            .mappings()
            .one_or_none()
        )
    if current is None or current["status"] != "running" or current["worker_resume_from"]:
        return
    try:
        saved, tool, job = admitted_parent(gateway, root, invocation_id, frozenset({"running"}))
        receipt = read_json(path, 131072)
        if (
            receipt.get("schema_version") != 1
            or receipt.get("invocation_id") != str(invocation_id)
            or receipt.get("job_sha256") != job["job_sha256"]
            or receipt.get("package_sha256") != tool.package_sha256
        ):
            raise ToolDenied("worker receipt does not match its exact job")
        status = receipt.get("status")
        if status == "approval_required":
            _record_wait(gateway, root, saved, job, receipt)
        elif status == "done":
            settings = gateway.live_text_settings
            assert settings is not None
            result = _checked_result(tool, receipt.get("result"), settings)
            settle(gateway, saved["project_id"], invocation_id, "done", result)
        elif status in {"failed", "unknown"}:
            nested_id = None
            if status == "unknown" and receipt.get("waiting_invocation_id"):
                candidate = UUID(receipt["waiting_invocation_id"])
                with gateway.engine.connect() as connection:
                    nested_id = connection.scalar(
                        text(
                            "SELECT id FROM extension_invocations WHERE project_id=:p AND id=:id "
                            "AND run_id=:run AND profile_id=:profile AND status='unknown' "
                            "AND call_id LIKE :prefix"
                        ),
                        {
                            "p": saved["project_id"],
                            "id": candidate,
                            "run": saved["run_id"],
                            "profile": saved["profile_id"],
                            "prefix": f"extension_cb_{invocation_id.hex}_%",
                        },
                    )
                if nested_id is None:
                    raise ToolDenied("unknown worker child is not an admitted callback")
            settle(
                gateway,
                saved["project_id"],
                invocation_id,
                status,
                reason="worker_execution_failed" if status == "failed" else "worker_outcome_unconfirmed",
                waiting_invocation_id=nested_id,
            )
        else:
            raise ToolDenied("worker receipt status is invalid")
    except (ToolDenied, ToolOutputRejected, ValueError, KeyError, OSError):
        settle(gateway, current["project_id"], invocation_id, "unknown", reason="worker_receipt_not_admitted")


def _copy_wait_evidence(root: Path, invocation_id: UUID, job: dict[str, Any]) -> Path:
    history = root / "history" / str(invocation_id)
    private_directory(history)
    history = history / str(job["job_sha256"])
    private_directory(history)
    for name in ("jobs", "claims", "receipts", "cancellations"):
        source, destination = root / name / f"{invocation_id}.json", history / f"{name}.json"
        if source.exists() and not destination.exists():
            os.link(source, destination)
    for name in ("requests", "responses"):
        directory = history / name
        private_directory(directory)
        for source in (root / "callbacks" / name).glob(f"{invocation_id}-*.json"):
            destination = directory / source.name
            if not destination.exists():
                os.link(source, destination)
    for directory in (history, history / "requests", history / "responses"):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return history


def _complete_resume(
    gateway: ToolGateway, root: Path, saved: dict[str, Any], tool: ExtensionTool, old_job: dict[str, Any]
) -> None:
    # Match cancellation's run -> invocation lock order. Filesystem changes
    # occur under these row locks and the host's attempts lock, never network I/O.
    with gateway.engine.begin() as connection:
        connection.execute(
            text("SELECT id FROM runs WHERE project_id=:p AND id=:run FOR UPDATE"),
            {"p": saved["project_id"], "run": saved["run_id"]},
        )
        current_row = (
            connection.execute(
                text(
                    "SELECT status,reason FROM extension_invocations "
                    "WHERE project_id=:p AND id=:id FOR UPDATE"
                ),
                {"p": saved["project_id"], "id": saved["id"]},
            )
            .mappings()
            .one()
        )
        cancellation = root / "cancellations" / f"{saved['id']}.json"
        if current_row["reason"] == "cancellation_requested" or cancellation.exists():
            if current_row["status"] == "running":
                connection.execute(
                    text(
                        "UPDATE extension_invocations SET status='cancelled',"
                        "reason='operator_cancelled',worker_resume_from=NULL,worker_resume_started_at=NULL,"
                        "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                    ),
                    {"p": saved["project_id"], "id": saved["id"]},
                )
                order_id = connection.scalar(
                    text("SELECT work_order_id FROM runs WHERE id=:run"), {"run": saved["run_id"]}
                )
                gateway._event(
                    connection,
                    saved["project_id"],
                    order_id,
                    saved["run_id"],
                    "extension_invocation_finished",
                    {
                        "invocation_id": str(saved["id"]),
                        "status": "cancelled",
                        "reason": "operator_cancelled",
                    },
                )
            return
        if current_row["status"] != "running":
            raise ToolDenied("worker resume is no longer running")
        current_path = root / "jobs" / f"{saved['id']}.json"
        current = read_json(current_path, 7 * 1024 * 1024) if current_path.exists() else None
        # A new job is published last; its presence proves prior cleanup completed.
        if current is None or current["job_sha256"] == old_job["job_sha256"]:
            for name in ("jobs", "claims", "receipts"):
                (root / name / f"{saved['id']}.json").unlink(missing_ok=True)
            for name in ("requests", "responses"):
                for path in (root / "callbacks" / name).glob(f"{saved['id']}-*.json"):
                    path.unlink()
            settings = gateway.live_text_settings
            assert settings is not None
            _worker_job(gateway, tool, saved, settings)
        else:
            admitted_parent(gateway, root, saved["id"], frozenset({"running"}))
        connection.execute(
            text(
                "UPDATE extension_invocations SET worker_resume_from=NULL,worker_resume_started_at=NULL,"
                "waiting_invocation_id=NULL,reason=NULL "
                "WHERE project_id=:p AND id=:id AND status='running' AND worker_resume_from=:hash"
            ),
            {"p": saved["project_id"], "id": saved["id"], "hash": old_job["job_sha256"]},
        )


def _recover_resumes(gateway: ToolGateway, root: Path) -> None:
    with gateway.engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT id,project_id,worker_resume_from FROM extension_invocations "
                    "WHERE status='running' "
                    "AND worker_resume_from IS NOT NULL"
                )
            ).mappings()
        )
    for row in rows:
        try:
            history = root / "history" / str(row["id"]) / row["worker_resume_from"]
            saved, tool, job = admitted_parent(
                gateway, root, row["id"], frozenset({"running"}), job_path=history / "jobs.json"
            )
            receipt = read_json(history / "receipts.json", 131072)
            if receipt.get("status") != "approval_required" or receipt.get("job_sha256") != job["job_sha256"]:
                raise ToolDenied("worker resume has no settled approval wait")
            _complete_resume(gateway, root, saved, tool, job)
        except (ToolDenied, ValueError, OSError, KeyError):
            cancellation = root / "cancellations" / f"{row['id']}.json"
            if not cancellation.exists():
                from onefoundry.gateway_extension_spool import publish_json

                publish_json(cancellation, {"schema_version": 1, "invocation_id": str(row["id"])})
            settle(gateway, row["project_id"], row["id"], "unknown", reason="worker_resume_not_admitted")


def resume_waiting(gateway: ToolGateway, root: Path) -> None:
    _settle_rejected_approvals(gateway)
    _settle_terminal_children(gateway)
    settings = gateway.live_text_settings
    if settings is None or not worker_status(settings)["available"]:
        return
    fd = os.open(root / ".attempts.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _recover_resumes(gateway, root)
        _resume_approved(gateway, root)
    finally:
        os.close(fd)


def _settle_rejected_approvals(gateway: ToolGateway) -> None:
    with gateway.engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT child.project_id,child.approval_id FROM extension_invocations child "
                    "JOIN approval_requests approval ON approval.project_id=child.project_id "
                    "AND approval.id=child.approval_id "
                    "WHERE child.status='approval_required' AND approval.status='rejected' "
                    "AND child.call_id LIKE 'extension_cb_%'"
                )
            ).mappings()
        )
    for row in rows:
        reject_callback_approval(gateway, row["project_id"], row["approval_id"])


def _settle_terminal_children(gateway: ToolGateway) -> None:
    with gateway.engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT parent.id,parent.project_id,nested.status FROM extension_invocations parent "
                    "JOIN extension_invocations nested ON nested.project_id=parent.project_id "
                    "AND nested.id=parent.waiting_invocation_id "
                    "WHERE parent.status='waiting' AND nested.status IN ('failed','unknown','cancelled')"
                )
            ).mappings()
        )
    for row in rows:
        status = "unknown" if row["status"] == "unknown" else "failed"
        try:
            settle(
                gateway,
                row["project_id"],
                row["id"],
                status,
                reason="connection_outcome_unconfirmed"
                if status == "unknown"
                else "connection_invocation_failed",
                expected_status="waiting",
            )
        except ToolOutcomeUnknown:
            # Cancellation or another reconciliation may already have settled this parent.
            continue


def _resume_approved(gateway: ToolGateway, root: Path) -> None:
    with gateway.engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT parent.id,parent.project_id,nested.status,approval.status AS approval_status "
                    "FROM extension_invocations parent JOIN extension_invocations nested "
                    "ON nested.project_id=parent.project_id AND nested.id=parent.waiting_invocation_id "
                    "LEFT JOIN approval_requests approval ON approval.project_id=nested.project_id "
                    "AND approval.id=nested.approval_id "
                    "WHERE parent.status='waiting'"
                )
            ).mappings()
        )
    for row in rows:
        if row["approval_status"] != "approved" and row["status"] != "done":
            continue
        if row["status"] not in {"approval_required", "done"}:
            continue
        try:
            saved, tool, job = admitted_parent(gateway, root, row["id"], frozenset({"waiting"}))
        except (ToolDenied, ValueError, OSError, KeyError):
            settle(
                gateway,
                row["project_id"],
                row["id"],
                "failed",
                reason="worker_wait_not_admitted",
                expected_status="waiting",
            )
            continue
        if tool.spec.runtime != "python" or tool.spec.effect != "pure":
            raise ToolDenied("only pure computation can resume a known connection wait")
        _copy_wait_evidence(root, row["id"], job)
        with gateway.engine.begin() as connection:
            updated = connection.execute(
                text(
                    "UPDATE extension_invocations SET status='running',worker_resume_from=:hash,"
                    "worker_resume_started_at=clock_timestamp() "
                    "WHERE id=:id AND project_id=:p AND status='waiting' RETURNING id"
                ),
                {"id": saved["id"], "p": saved["project_id"], "hash": job["job_sha256"]},
            ).one_or_none()
            if updated is None:
                raise ToolDenied("worker approval wait changed")
        _complete_resume(gateway, root, saved, tool, job)
