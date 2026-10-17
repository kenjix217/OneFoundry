"""Explicit, one-shot fake Codex dispatch from an approved immutable snapshot."""

# Fixed SQL fragments below; all request values are bound.
# ruff: noqa: S608

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry import run_audit
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.github_snapshot_codex_contract import manifest_payload
from onefoundry.input_file_runner import prepare_runner_context, record_runner_context
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.runner_spool import SpoolInvalid
from onefoundry.workflow_schemas import SECRET_PATTERN

if TYPE_CHECKING:
    from onefoundry.execution_gateway import ExecutionGateway

TARGET = "github_snapshot_codex_fixture_v1"


def eligibility(execution: ExecutionGateway, project_id: UUID, order_id: UUID) -> dict[str, Any]:
    with execution.gateway.engine.connect() as connection:
        order = (
            connection.execute(
                text(
                    "SELECT w.status,p.status AS project_status,pr.kind,pr.name "
                    "FROM work_orders w JOIN projects p ON p.id=w.project_id "
                    "JOIN profiles pr ON pr.project_id=w.project_id AND pr.id=w.profile_id "
                    "WHERE w.project_id=:project_id AND w.id=:order_id"
                ),
                {"project_id": project_id, "order_id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        rows = (
            connection.execute(
                text(
                    "SELECT s.attempt_id AS snapshot_attempt_id,s.approval_id,s.source_id,s.account_id,"
                    "s.owner,s.repo,s.branch,s.saved_revision,s.source_tree,s.snapshot_path,"
                    "s.snapshot_sha256,s.snapshot_byte_size,s.receipt_sha256 "
                    "FROM github_fixture_source_snapshots s "
                    "JOIN approval_requests a ON a.project_id=s.project_id AND a.id=s.approval_id "
                    "JOIN project_sources src ON src.project_id=s.project_id AND src.id=s.source_id "
                    "JOIN github_accounts ga ON ga.project_id=s.project_id AND ga.id=s.account_id "
                    "WHERE s.project_id=:project_id AND a.status='approved' AND src.kind='github' "
                    "AND src.account_id=s.account_id AND src.owner=s.owner AND src.repo=s.repo "
                    "AND src.branch=s.branch AND src.revision=s.saved_revision "
                    "AND EXISTS (SELECT 1 FROM events e WHERE e.project_id=s.project_id "
                    "AND e.run_id=s.run_id AND e.event_type='github_fixture_snapshot_receipt' "
                    "AND e.payload->>'approval_id'=s.approval_id::text "
                    "AND e.payload->>'attempt_id'=s.attempt_id::text "
                    "AND e.payload->>'receipt_sha256'=s.receipt_sha256) "
                    "ORDER BY s.created_at DESC,s.attempt_id LIMIT 25"
                ),
                {"project_id": project_id},
            )
            .mappings()
            .all()
        )
    choices = [dict(row) for row in rows if _credential_present(execution, row["account_id"])]
    reason: str | None = None
    if (
        order["project_status"] != "active"
        or order["status"] != "ready"
        or (order["kind"], order["name"]) != ("builtin", "Engineer")
    ):
        reason = "Select a separate ready built-in Engineer work order in an active project."
    elif not choices:
        reason = "No approved current synthetic GitHub snapshot is available."
    else:
        try:
            _capability(execution)
        except Unavailable as exc:
            reason = str(exc)
    return {"eligible": reason is None, "reason": reason, "snapshots": choices}


def _intent_sha(project_id: UUID, order_id: UUID, snapshot_id: UUID, key: UUID) -> str:
    value = {
        "adapter_id": "codex_cli",
        "idempotency_key": str(key),
        "project_id": str(project_id),
        "snapshot_attempt_id": str(snapshot_id),
        "target_id": TARGET,
        "work_order_id": str(order_id),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _capability(execution: ExecutionGateway) -> None:
    """The regular live capability never admits this separate synthetic path."""
    try:
        raw = execution.spool._read_regular(execution.spool.root / "snapshot_codex_capabilities.json", 4096)
        value = json.loads(raw)
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        age = (datetime.now(UTC) - observed).total_seconds()
        if (
            set(value)
            != {"schema_version", "observed_at", "available", "synthetic_only", "brokered", "isolated"}
            or value["schema_version"] != 1
            or value["available"] is not True
            or value["synthetic_only"] is not True
            or value["brokered"] is not True
            or value["isolated"] is not True
            or observed.tzinfo is None
            or not 0 <= age <= execution.capability_max_age_seconds
        ):
            raise ValueError("stale or incomplete capability")
        fd = os.open(execution.spool.root / ".snapshot-codex.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("supervisor lock invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                raise ValueError("supervisor is stopped")
        finally:
            os.close(fd)
    except (OSError, SpoolInvalid, ValueError, TypeError, KeyError, AttributeError):
        raise Unavailable("Synthetic snapshot Codex runner is unavailable") from None


def _prior(connection: Any, project_id: UUID, key: UUID) -> Any:
    return (
        connection.execute(
            text(
                "SELECT a.run_id,a.snapshot_attempt_id,a.intent_sha256,r.work_order_id "
                "FROM external_code_attempts a JOIN runs r ON r.project_id=a.project_id "
                "AND r.id=a.run_id WHERE a.project_id=:project_id AND a.idempotency_key=:key"
            ),
            {"project_id": project_id, "key": key},
        )
        .mappings()
        .one_or_none()
    )


def _credential_present(execution: ExecutionGateway, account_id: UUID) -> bool:
    settings = execution.gateway.live_text_settings
    if settings is None:
        return False
    try:
        return ModelSecretStore(str(Path(settings.model_secret_root) / "github")).present(account_id)
    except (OSError, SecretStoreError):
        return False


def launch(
    execution: ExecutionGateway, project_id: UUID, order_id: UUID, snapshot_id: UUID, key: UUID
) -> dict[str, Any]:
    intent_sha = _intent_sha(project_id, order_id, snapshot_id, key)
    with execution.gateway.engine.connect() as connection:
        prior = _prior(connection, project_id, key)
    if prior is not None:
        if (
            prior["work_order_id"] != order_id
            or prior["snapshot_attempt_id"] != snapshot_id
            or prior["intent_sha256"] != intent_sha
        ):
            raise Conflict("idempotency key is already bound to another coding request")
        return execution.detail(project_id, prior["run_id"])
    _capability(execution)
    run_id, attempt_id = uuid4(), uuid4()
    with execution.gateway.engine.begin() as connection:
        advisory_bytes = hashlib.sha256(f"{project_id}:{key}".encode()).digest()[:8]
        advisory_key = int.from_bytes(advisory_bytes, "big", signed=True)
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:advisory_key)"), {"advisory_key": advisory_key}
        )
        project_status = connection.scalar(
            text("SELECT status FROM projects WHERE id=:project_id FOR SHARE"),
            {"project_id": project_id},
        )
        order = (
            connection.execute(
                text(
                    "SELECT w.status,w.profile_id,w.title,w.instructions,p.status AS project_status "
                    "FROM work_orders w JOIN projects p ON p.id=w.project_id "
                    "WHERE w.project_id=:project_id AND w.id=:order_id FOR UPDATE OF w"
                ),
                {"project_id": project_id, "order_id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        prior = _prior(connection, project_id, key)
        if prior is not None:
            if (
                prior["work_order_id"] != order_id
                or prior["snapshot_attempt_id"] != snapshot_id
                or prior["intent_sha256"] != intent_sha
            ):
                raise Conflict("idempotency key is already bound to another coding request")
            return execution.detail(project_id, prior["run_id"])
        engineer = connection.scalar(
            text(
                "SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:profile_id "
                "AND kind='builtin' AND name='Engineer'"
            ),
            {"project_id": project_id, "profile_id": order["profile_id"]},
        )
        if project_status != "active" or order["status"] != "ready" or engineer is None:
            raise Conflict("select a separate ready built-in Engineer work order in an active project")
        if (
            SECRET_PATTERN.search(order["title"])
            or SECRET_PATTERN.search(order["instructions"])
            or len(order["title"].encode()) + len(order["instructions"].encode()) > 6000
        ):
            raise Conflict("work order is not safe for the bounded synthetic manifest")
        snapshot = (
            connection.execute(
                text(
                    "SELECT s.*,a.status AS approval_status,src.kind AS source_kind,"
                    "src.account_id AS current_account_id,src.owner AS current_owner,"
                    "src.repo AS current_repo,src.branch AS current_branch,"
                    "src.revision AS current_revision "
                    "FROM github_fixture_source_snapshots s "
                    "JOIN approval_requests a ON a.project_id=s.project_id AND a.id=s.approval_id "
                    "JOIN project_sources src ON src.project_id=s.project_id AND src.id=s.source_id "
                    "JOIN github_accounts ga ON ga.project_id=s.project_id AND ga.id=s.account_id "
                    "WHERE s.project_id=:project_id AND s.attempt_id=:snapshot_id FOR SHARE OF src"
                ),
                {"project_id": project_id, "snapshot_id": snapshot_id},
            )
            .mappings()
            .one_or_none()
        )
        if snapshot is None:
            raise NotFound("synthetic snapshot not found in this project")
        if (
            snapshot["approval_status"] != "approved"
            or snapshot["source_kind"] != "github"
            or snapshot["account_id"] != snapshot["current_account_id"]
            or snapshot["owner"] != snapshot["current_owner"]
            or snapshot["repo"] != snapshot["current_repo"]
            or snapshot["branch"] != snapshot["current_branch"]
            or snapshot["saved_revision"] != snapshot["current_revision"]
            or not _credential_present(execution, snapshot["account_id"])
        ):
            raise Conflict("synthetic snapshot source or account is no longer current")
        receipt_event = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:fetch_run_id "
                "AND event_type='github_fixture_snapshot_receipt' "
                "AND payload->>'approval_id'=:approval_id "
                "AND payload->>'attempt_id'=:snapshot_id "
                "AND payload->>'receipt_sha256'=:receipt_sha LIMIT 1"
            ),
            {
                "project_id": project_id,
                "fetch_run_id": snapshot["run_id"],
                "approval_id": str(snapshot["approval_id"]),
                "snapshot_id": str(snapshot_id),
                "receipt_sha": snapshot["receipt_sha256"],
            },
        )
        if receipt_event is None:
            raise Conflict("synthetic fetch receipt lineage is unavailable")
        source = {
            "snapshot_attempt_id": str(snapshot_id),
            "source_id": str(snapshot["source_id"]),
            "account_id": str(snapshot["account_id"]),
            "owner": snapshot["owner"],
            "repo": snapshot["repo"],
            "branch": snapshot["branch"],
            "saved_revision": snapshot["saved_revision"],
            "source_tree": snapshot["source_tree"],
            "snapshot_path": snapshot["snapshot_path"],
            "snapshot_sha256": snapshot["snapshot_sha256"],
            "snapshot_byte_size": snapshot["snapshot_byte_size"],
            "snapshot_request_sha256": snapshot["request_sha256"],
            "snapshot_receipt_sha256": snapshot["receipt_sha256"],
        }
        settings = execution.gateway.live_text_settings
        if settings is None:
            raise Unavailable("input-file configuration unavailable")
        task_instructions, selected_files = prepare_runner_context(
            connection, settings, project_id, order_id, order["instructions"]
        )
        unsigned: dict[str, object] = {
            "schema_version": 3,
            "target_id": TARGET,
            "job_id": str(attempt_id),
            "project_id": str(project_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "work_order_id": str(order_id),
            "idempotency_key": str(key),
            "intent_sha256": intent_sha,
            "adapter_id": "codex_cli",
            "stage": "engineer",
            "actor_id": execution.gateway.actor_id,
            "task_title": order["title"],
            "task_instructions": task_instructions,
            "source": source,
            "limits": execution.limits,
        }
        manifest = manifest_payload(unsigned)
        if len(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()) > 8192:
            raise Conflict("synthetic coding manifest exceeds bound")
        run_audit.insert_run(
            connection,
            project_id,
            run_id,
            order_id,
            order["profile_id"],
            execution.gateway.actor_id,
            "external_code",
        )
        record_runner_context(connection, project_id, run_id, execution.gateway.actor_id, selected_files)
        connection.execute(
            text("UPDATE work_orders SET status='running' WHERE project_id=:project_id AND id=:id"),
            {"project_id": project_id, "id": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO external_code_attempts "
                "(id,project_id,run_id,actor_id,adapter_id,fixture_id,request_sha256,status,"
                "snapshot_attempt_id,idempotency_key,intent_sha256,snapshot_source_id,"
                "snapshot_account_id,snapshot_owner,snapshot_repo,snapshot_branch,"
                "snapshot_revision,snapshot_tree,snapshot_path,snapshot_sha256,"
                "snapshot_byte_size,snapshot_request_sha256,snapshot_receipt_sha256) "
                "VALUES (:id,:project_id,:run_id,:actor_id,'codex_cli',:target,:request_sha256,'queued',"
                ":snapshot_attempt_id,:idempotency_key,:intent_sha256,:source_id,:account_id,"
                ":owner,:repo,:branch,:saved_revision,:source_tree,:snapshot_path,:snapshot_sha256,:snapshot_byte_size,"
                ":snapshot_request_sha256,:snapshot_receipt_sha256)"
            ),
            {
                "id": attempt_id,
                "project_id": project_id,
                "run_id": run_id,
                "actor_id": execution.gateway.actor_id,
                "target": TARGET,
                "request_sha256": manifest["request_sha256"],
                "idempotency_key": key,
                "intent_sha256": intent_sha,
                **source,
                "snapshot_attempt_id": snapshot_id,
                "source_id": UUID(source["source_id"]),
                "account_id": UUID(source["account_id"]),
            },
        )
        execution.gateway._event(
            connection,
            project_id,
            order_id,
            run_id,
            "external_code_queued",
            {
                "attempt_id": str(attempt_id),
                "target_id": TARGET,
                "adapter_id": "codex_cli",
                "snapshot_attempt_id": str(snapshot_id),
                "snapshot_sha256": source["snapshot_sha256"],
                "source_tree": source["source_tree"],
                "saved_revision": source["saved_revision"],
                "intent_sha256": intent_sha,
                "request_sha256": manifest["request_sha256"],
                "mode": "synthetic_only_dummy_broker",
            },
        )
    try:
        execution.spool.write_job(attempt_id, manifest)
    except (OSError, SpoolInvalid):
        execution._mark_unknown(project_id, run_id, attempt_id, "manifest_dispatch_uncertain")
    return execution.detail(project_id, run_id)
