"""Tool Gateway owner for one approved offline GitHub stage spool attempt."""

# SQL fragments below are constants; all request values use binds.
# ruff: noqa: S608

import fcntl
import hashlib
import os
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.github_stage_contract import StageManifest, StageReceipt, canonical, manifest_payload
from onefoundry.github_stage_proposal import bound_source_state
from onefoundry.github_stage_proposal import status as proposal_status
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _spool(gateway: "ToolGateway") -> RunnerSpool:
    if gateway.runner_spool is None:
        raise Unavailable("offline GitHub stage spool unavailable")
    spool = RunnerSpool(str(gateway.runner_spool.root / "github-stage"))
    try:
        for name in ("jobs", "claims", "receipts", "artifacts"):
            spool._directory(name)
    except SpoolInvalid:
        raise Unavailable("offline GitHub stage supervisor is not configured") from None
    return spool


def _require_supervisor(spool: RunnerSpool) -> None:
    """An OS lock proves an opt-in supervisor process is currently attached."""
    path = spool.root / ".supervisor.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise Unavailable("offline synthetic stage supervisor unavailable")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    except OSError:
        pass
    raise Unavailable("offline synthetic stage supervisor unavailable")


def _readback_job(spool: RunnerSpool, attempt_id: UUID, expected: dict[str, Any]) -> None:
    path = spool._directory("jobs") / f"{attempt_id}.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError("offline stage job invalid")
        raw = os.read(descriptor, 8193)
        if len(raw) != info.st_size:
            raise ValueError("offline stage job changed")
        observed = StageManifest.model_validate_json(raw)
        if (
            observed.model_dump() != expected
            or hashlib.sha256(raw).hexdigest() != hashlib.sha256(canonical(expected)).hexdigest()
        ):
            raise ValueError("offline stage job readback mismatch")
    finally:
        os.close(descriptor)


def _events(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT event_type,payload,actor_id,run_id,created_at FROM events "
                    "WHERE project_id=:project_id AND payload->>'approval_id'=:approval_id "
                    "AND event_type IN ('github_stage_queued','github_stage_receipt','github_stage_unknown') "
                    "ORDER BY created_at,id"
                ),
                {"project_id": project_id, "approval_id": str(approval_id)},
            ).mappings()
        ]


def _terminal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(events):
        if item["event_type"] in {"github_stage_receipt", "github_stage_unknown"}:
            return item
    return None


def _record_terminal(
    gateway: "ToolGateway",
    project_id: UUID,
    approval_id: UUID,
    queued: dict[str, Any],
    *,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    with gateway.engine.begin() as connection:
        approval = (
            connection.execute(
                text(
                    "SELECT run_id,work_order_id FROM approval_requests "
                    "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                ),
                {"project_id": project_id, "id": approval_id},
            )
            .mappings()
            .one_or_none()
        )
        if approval is None:
            return
        existing = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:project_id AND "
                "payload->>'approval_id'=:approval_id AND "
                "event_type IN ('github_stage_receipt','github_stage_unknown') LIMIT 1"
            ),
            {"project_id": project_id, "approval_id": str(approval_id)},
        )
        if existing is not None:
            return
        gateway._event(
            connection,
            project_id,
            approval["work_order_id"],
            approval["run_id"],
            event_type,
            {"approval_id": str(approval_id), "attempt_id": queued["payload"]["attempt_id"], **payload},
        )


def dispatch(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    """Consume an approved proposal once; only a secret-free manifest leaves the app."""
    spool = _spool(gateway)
    attempt_id = uuid4()
    with gateway.engine.begin() as connection:
        approval = (
            connection.execute(
                text(
                    "SELECT id,run_id,work_order_id,status,action_type,target,content,proposal_digest "
                    "FROM approval_requests WHERE project_id=:project_id AND id=:id FOR UPDATE"
                ),
                {"project_id": project_id, "id": approval_id},
            )
            .mappings()
            .one_or_none()
        )
        if approval is None:
            raise NotFound("GitHub stage proposal not found")
        if approval["status"] != "approved":
            raise Conflict("GitHub stage proposal must be approved")
        bound = bound_source_state(connection, project_id, approval, lock=True)
        if bound is None:
            raise NotFound("GitHub stage proposal not found")
        exact, fresh = bound
        if not fresh:
            raise Conflict("saved GitHub source changed; offline stage not dispatched")
        prior = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:project_id "
                "AND event_type='github_stage_queued' AND payload->>'approval_id'=:id LIMIT 1"
            ),
            {"project_id": project_id, "id": str(approval_id)},
        )
        if prior is not None:
            raise Conflict("GitHub stage proposal already dispatched")
        _require_supervisor(spool)
        manifest = manifest_payload(
            job_id=attempt_id,
            project_id=project_id,
            run_id=approval["run_id"],
            approval_id=approval_id,
            actor_id=gateway.actor_id,
            proposal_digest=approval["proposal_digest"],
            source=exact,
        )
        gateway._event(
            connection,
            project_id,
            approval["work_order_id"],
            approval["run_id"],
            "github_stage_queued",
            {
                "approval_id": str(approval_id),
                "attempt_id": str(attempt_id),
                "request_sha256": manifest["request_sha256"],
                "proposal_digest": approval["proposal_digest"],
                "saved_revision": exact["saved_revision"],
                "mode": "offline_synthetic_fixture",
            },
        )
    try:
        spool.write_job(attempt_id, manifest)
        _readback_job(spool, attempt_id, manifest)
    except (OSError, SpoolInvalid, ValueError, ValidationError):
        queued = _events(gateway, project_id, approval_id)[0]
        _record_terminal(
            gateway,
            project_id,
            approval_id,
            queued,
            event_type="github_stage_unknown",
            payload={"reason_code": "manifest_dispatch_uncertain"},
        )
    return read_status(gateway, project_id, approval_id)


def _validate_receipt(
    receipt: StageReceipt, approval_id: UUID, queued: dict[str, Any], exact: dict[str, str]
) -> None:
    payload = queued["payload"]
    if (
        receipt.job_id != payload["attempt_id"]
        or receipt.approval_id != str(approval_id)
        or receipt.project_id != str(queued["project_id"])
        or receipt.run_id != str(queued["run_id"])
        or receipt.request_sha256 != payload["request_sha256"]
        or receipt.proposal_digest != payload["proposal_digest"]
        or receipt.source.model_dump() != exact
    ):
        raise ValueError("offline stage receipt binding mismatch")


def read_status(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    """Read spool owner state once and append a validated terminal event."""
    result = proposal_status(gateway, project_id, approval_id)
    events = _events(gateway, project_id, approval_id)
    queued = next((item for item in events if item["event_type"] == "github_stage_queued"), None)
    if queued is None:
        return {**result, "stage_status": "not_launched", "mode": "offline_synthetic_fixture"}
    queued = {**queued, "project_id": project_id}
    terminal = _terminal(events)
    if terminal is None:
        attempt_id = UUID(queued["payload"]["attempt_id"])
        try:
            spool = _spool(gateway)
            observed = spool.read_receipt(attempt_id)
            if observed is not None:
                receipt_value, receipt_digest = observed
                receipt = StageReceipt.model_validate(receipt_value)
                _validate_receipt(receipt, approval_id, queued, result["source"])
                _record_terminal(
                    gateway,
                    project_id,
                    approval_id,
                    queued,
                    event_type="github_stage_receipt",
                    payload={
                        "status": receipt.status,
                        "reason_code": receipt.reason_code,
                        "receipt_sha256": receipt_digest,
                        "source_tree": receipt.source_tree,
                        "file_count": receipt.file_count,
                        "total_bytes": receipt.total_bytes,
                    },
                )
            else:
                wait = gateway.runner_receipt_wait_seconds
                if wait is None:
                    raise SpoolInvalid("receipt wait unavailable")
                age = spool.claim_age_seconds(attempt_id)
                if age is not None and age >= wait:
                    _record_terminal(
                        gateway,
                        project_id,
                        approval_id,
                        queued,
                        event_type="github_stage_unknown",
                        payload={"reason_code": "claimed_receipt_missing"},
                    )
                elif age is None and (datetime.now(UTC) - queued["created_at"]).total_seconds() >= wait:
                    if spool.cancel_unclaimed(attempt_id):
                        _record_terminal(
                            gateway,
                            project_id,
                            approval_id,
                            queued,
                            event_type="github_stage_unknown",
                            payload={"reason_code": "unclaimed_dispatch_expired"},
                        )
        except (OSError, SpoolInvalid, Unavailable, ValidationError, ValueError, KeyError):
            _record_terminal(
                gateway,
                project_id,
                approval_id,
                queued,
                event_type="github_stage_unknown",
                payload={"reason_code": "receipt_invalid"},
            )
        terminal = _terminal(_events(gateway, project_id, approval_id))
    stage_status = terminal["payload"].get("status", "unknown") if terminal else "queued"
    return {
        **result,
        "stage_status": stage_status,
        "mode": "offline_synthetic_fixture",
        "attempt_id": queued["payload"]["attempt_id"],
        "receipt_sha256": terminal["payload"].get("receipt_sha256") if terminal else None,
        "source_tree": terminal["payload"].get("source_tree") if terminal else None,
        "file_count": terminal["payload"].get("file_count") if terminal else None,
        "total_bytes": terminal["payload"].get("total_bytes") if terminal else None,
        "reason_code": terminal["payload"].get("reason_code") if terminal else None,
    }
