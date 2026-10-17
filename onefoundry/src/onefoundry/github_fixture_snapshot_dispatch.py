"""One-shot Tool Gateway dispatch and receipt readback for local HTTPS fixtures."""

import fcntl
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.github_fixture_snapshot_contract import FetchManifest, FetchReceipt, expiry, manifest_payload
from onefoundry.github_fixture_snapshot_proposal import bound_intent
from onefoundry.github_fixture_snapshot_proposal import status as proposal_status
from onefoundry.github_snapshot_bundle import BundleInvalid, read_snapshot
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _spool(gateway: "ToolGateway") -> RunnerSpool:
    if gateway.runner_spool is None:
        raise Unavailable("fixture HTTPS fetch spool unavailable")
    spool = RunnerSpool(str(gateway.runner_spool.root / "github-fixture-https-snapshot-v3"))
    try:
        for name in ("jobs", "claims", "receipts", "artifacts"):
            spool._directory(name)
    except SpoolInvalid:
        raise Unavailable("fixture HTTPS fetch supervisor is not configured") from None
    return spool


def _snapshot_root(gateway: "ToolGateway") -> Path:
    if gateway.live_text_settings is None:
        raise Unavailable("fixture snapshot storage unavailable")
    root = Path(gateway.live_text_settings.github_snapshot_root)
    directory = root / "snapshots"
    if root.is_symlink() or not root.is_dir() or directory.is_symlink() or not directory.is_dir():
        raise Unavailable("fixture snapshot storage unavailable")
    return root


def _require_supervisor(spool: RunnerSpool) -> None:
    try:
        fd = os.open(spool.root / ".supervisor.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unavailable("fixture HTTPS fetch supervisor unavailable")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError:
        pass
    raise Unavailable("fixture HTTPS fetch supervisor unavailable")


def _events(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT event_type,payload,run_id,created_at FROM events "
                    "WHERE project_id=:project_id AND payload->>'approval_id'=:approval_id "
                    "AND event_type IN ('github_fixture_snapshot_queued','github_fixture_snapshot_receipt',"
                    "'github_fixture_snapshot_unknown') ORDER BY created_at,id"
                ),
                {"project_id": project_id, "approval_id": str(approval_id)},
            ).mappings()
        ]


def _terminal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if event["event_type"] in {"github_fixture_snapshot_receipt", "github_fixture_snapshot_unknown"}
        ),
        None,
    )


def _record_terminal(
    gateway: "ToolGateway",
    project_id: UUID,
    approval_id: UUID,
    queued: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    snapshot: FetchReceipt | None = None,
) -> None:
    with gateway.engine.begin() as connection:
        approval = (
            connection.execute(
                text(
                    "SELECT id,run_id,work_order_id,actor_id,action_type,target,content,"
                    "proposal_digest,status "
                    "FROM approval_requests "
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
                "SELECT 1 FROM events WHERE project_id=:project_id "
                "AND payload->>'approval_id'=:approval_id "
                "AND event_type IN ('github_fixture_snapshot_receipt',"
                "'github_fixture_snapshot_unknown') LIMIT 1"
            ),
            {"project_id": project_id, "approval_id": str(approval_id)},
        )
        if existing is None:
            if snapshot is not None:
                bound = bound_intent(connection, project_id, approval, lock=True)
                if bound is None or not bound[1] or approval["status"] != "approved":
                    event_type = "github_fixture_snapshot_unknown"
                    payload = {"reason_code": "source_no_longer_current"}
                else:
                    intent = bound[0]
                    connection.execute(
                        text(
                            "INSERT INTO github_fixture_source_snapshots "
                            "(attempt_id,project_id,run_id,approval_id,actor_id,source_id,account_id,"
                            "owner,repo,branch,saved_revision,source_tree,file_count,total_bytes,"
                            "snapshot_path,snapshot_sha256,snapshot_byte_size,request_sha256,receipt_sha256) "
                            "VALUES (:attempt_id,:project_id,:run_id,:approval_id,:actor_id,:source_id,"
                            ":account_id,:owner,:repo,:branch,:saved_revision,:source_tree,:file_count,"
                            ":total_bytes,:snapshot_path,:snapshot_sha256,:snapshot_byte_size,"
                            ":request_sha256,:receipt_sha256)"
                        ),
                        {
                            "attempt_id": UUID(queued["payload"]["attempt_id"]),
                            "project_id": project_id,
                            "run_id": approval["run_id"],
                            "approval_id": approval_id,
                            "actor_id": approval["actor_id"],
                            "source_id": UUID(intent.source_id),
                            "account_id": UUID(intent.account_id),
                            "owner": intent.owner,
                            "repo": intent.repo,
                            "branch": intent.branch,
                            "saved_revision": intent.saved_revision,
                            "source_tree": snapshot.source_tree,
                            "file_count": snapshot.file_count,
                            "total_bytes": snapshot.total_bytes,
                            "snapshot_path": snapshot.snapshot_path,
                            "snapshot_sha256": snapshot.snapshot_sha256,
                            "snapshot_byte_size": snapshot.snapshot_byte_size,
                            "request_sha256": snapshot.request_sha256,
                            "receipt_sha256": payload["receipt_sha256"],
                        },
                    )
            gateway._event(
                connection,
                project_id,
                approval["work_order_id"],
                approval["run_id"],
                event_type,
                {"approval_id": str(approval_id), "attempt_id": queued["payload"]["attempt_id"], **payload},
            )


def dispatch(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    spool = _spool(gateway)
    _snapshot_root(gateway)
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
            raise NotFound("fixture fetch proposal not found")
        bound = bound_intent(connection, project_id, approval, lock=True)
        if bound is None:
            raise NotFound("fixture fetch proposal not found")
        intent, fresh = bound
        if approval["status"] != "approved":
            raise Conflict("fixture fetch proposal must be approved")
        if not fresh:
            raise Conflict("saved GitHub source changed; fixture fetch not dispatched")
        if datetime.now(UTC) >= expiry(intent.expires_at):
            raise Conflict("fixture fetch approval expired")
        prior = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:project_id "
                "AND event_type='github_fixture_snapshot_queued' "
                "AND payload->>'approval_id'=:id LIMIT 1"
            ),
            {"project_id": project_id, "id": str(approval_id)},
        )
        if prior is not None:
            raise Conflict("fixture fetch already dispatched")
        _require_supervisor(spool)
        manifest = manifest_payload(
            job_id=attempt_id,
            project_id=project_id,
            run_id=approval["run_id"],
            approval_id=approval_id,
            actor_id=gateway.actor_id,
            proposal_digest=approval["proposal_digest"],
            intent=intent,
        )
        gateway._event(
            connection,
            project_id,
            approval["work_order_id"],
            approval["run_id"],
            "github_fixture_snapshot_queued",
            {
                "approval_id": str(approval_id),
                "attempt_id": str(attempt_id),
                "request_sha256": manifest["request_sha256"],
                "proposal_digest": approval["proposal_digest"],
                "idempotency_key": intent.idempotency_key,
                "expires_at": intent.expires_at,
                "action": intent.action,
                "mode": "synthetic_local_https_snapshot_fixture_asserted_commit",
            },
        )
    try:
        spool.write_job(attempt_id, manifest)
        observed = spool._read_regular(spool._directory("jobs") / f"{attempt_id}.json", 8192)
        if FetchManifest.model_validate_json(observed).model_dump() != manifest:
            raise ValueError("fixture fetch manifest readback mismatch")
    except (OSError, SpoolInvalid, ValueError, ValidationError):
        queued = _events(gateway, project_id, approval_id)[0]
        _record_terminal(
            gateway,
            project_id,
            approval_id,
            queued,
            "github_fixture_snapshot_unknown",
            {"reason_code": "manifest_dispatch_uncertain"},
        )
    return read_status(gateway, project_id, approval_id)


def _validate_receipt(
    receipt: FetchReceipt, project_id: UUID, approval_id: UUID, queued: dict[str, Any], status: dict[str, Any]
) -> None:
    payload = queued["payload"]
    if (
        receipt.job_id != payload["attempt_id"]
        or receipt.project_id != str(project_id)
        or receipt.run_id != str(queued["run_id"])
        or receipt.approval_id != str(approval_id)
        or receipt.proposal_digest != payload["proposal_digest"]
        or receipt.request_sha256 != payload["request_sha256"]
        or receipt.idempotency_key != status["idempotency_key"]
        or receipt.expires_at != status["expires_at"]
        or receipt.source.model_dump() != status["source"]
    ):
        raise ValueError("fixture fetch receipt binding mismatch")


def read_status(gateway: "ToolGateway", project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    result = proposal_status(gateway, project_id, approval_id)
    events = _events(gateway, project_id, approval_id)
    queued = next(
        (event for event in events if event["event_type"] == "github_fixture_snapshot_queued"), None
    )
    if queued is None:
        return {**result, "mode": "synthetic_local_https_snapshot_fixture_asserted_commit"}
    terminal = _terminal(events)
    if terminal is None:
        attempt_id = UUID(queued["payload"]["attempt_id"])
        try:
            spool = _spool(gateway)
            observed = spool.read_receipt(attempt_id)
            if observed is not None:
                value, receipt_digest = observed
                receipt = FetchReceipt.model_validate(value)
                _validate_receipt(receipt, project_id, approval_id, queued, result)
                if receipt.status == "done":
                    assert receipt.snapshot_path is not None
                    assert receipt.snapshot_sha256 is not None
                    assert receipt.snapshot_byte_size is not None
                    stored = read_snapshot(
                        _snapshot_root(gateway),
                        receipt.snapshot_path,
                        receipt.snapshot_sha256,
                        receipt.snapshot_byte_size,
                    )
                    if (
                        stored.source_tree != receipt.source_tree
                        or len(stored.files) != receipt.file_count
                        or stored.total_bytes != receipt.total_bytes
                    ):
                        raise BundleInvalid("fixture snapshot receipt content mismatch")
                _record_terminal(
                    gateway,
                    project_id,
                    approval_id,
                    queued,
                    "github_fixture_snapshot_receipt",
                    {
                        "status": receipt.status,
                        "reason_code": receipt.reason_code,
                        "receipt_sha256": receipt_digest,
                        "source_revision": receipt.source_revision,
                        "source_tree": receipt.source_tree,
                        "file_count": receipt.file_count,
                        "total_bytes": receipt.total_bytes,
                        "snapshot_path": receipt.snapshot_path,
                        "snapshot_sha256": receipt.snapshot_sha256,
                        "snapshot_byte_size": receipt.snapshot_byte_size,
                    },
                    receipt if receipt.status == "done" else None,
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
                        "github_fixture_snapshot_unknown",
                        {"reason_code": "claimed_receipt_missing"},
                    )
                elif age is None and (datetime.now(UTC) - queued["created_at"]).total_seconds() >= wait:
                    if spool.cancel_unclaimed(attempt_id):
                        _record_terminal(
                            gateway,
                            project_id,
                            approval_id,
                            queued,
                            "github_fixture_snapshot_unknown",
                            {"reason_code": "unclaimed_dispatch_expired"},
                        )
        except (OSError, SpoolInvalid, Unavailable, ValidationError, ValueError, KeyError):
            _record_terminal(
                gateway,
                project_id,
                approval_id,
                queued,
                "github_fixture_snapshot_unknown",
                {"reason_code": "receipt_invalid"},
            )
        terminal = _terminal(_events(gateway, project_id, approval_id))
    return {
        **result,
        "snapshot_status": terminal["payload"].get("status", "unknown") if terminal else "queued",
        "mode": "synthetic_local_https_snapshot_fixture_asserted_commit",
        "attempt_id": queued["payload"]["attempt_id"],
        "receipt_sha256": terminal["payload"].get("receipt_sha256") if terminal else None,
        "source_revision": terminal["payload"].get("source_revision") if terminal else None,
        "source_tree": terminal["payload"].get("source_tree") if terminal else None,
        "file_count": terminal["payload"].get("file_count") if terminal else None,
        "total_bytes": terminal["payload"].get("total_bytes") if terminal else None,
        "snapshot_path": terminal["payload"].get("snapshot_path") if terminal else None,
        "snapshot_sha256": terminal["payload"].get("snapshot_sha256") if terminal else None,
        "snapshot_byte_size": terminal["payload"].get("snapshot_byte_size") if terminal else None,
        "reason_code": terminal["payload"].get("reason_code") if terminal else None,
    }
