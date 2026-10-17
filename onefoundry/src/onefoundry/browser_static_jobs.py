"""Gateway-owned browser job intent and exact host receipt reconciliation.

This module never launches Docker. The host process consumes the separate
browser spool; a claimed job with uncertain receipt is never resent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.browser_static_fixtures import StaticFixtureStore
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_sensitive_text import contains_sensitive_text
from runner.browser_static_job_spool import BrowserJobError, BrowserStaticJobSpool

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,159}\Z")
_PUBLIC_FIELDS = (
    "id",
    "project_id",
    "run_id",
    "fixture_id",
    "actor_id",
    "status",
    "reason_code",
    "request_sha256",
    "binding_digest",
    "source_sha256",
    "image_id",
    "receipt_sha256",
    "created_at",
    "finished_at",
)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in _PUBLIC_FIELDS}


class BrowserStaticJobStore:
    """Persist one exact dispatch request and one terminal observation."""

    def __init__(
        self,
        gateway: ToolGateway,
        fixtures: StaticFixtureStore,
        spool: BrowserStaticJobSpool,
        image_id: str,
        claim_timeout_seconds: int,
    ) -> None:
        if not isinstance(image_id, str) or _IMAGE.fullmatch(image_id) is None:
            raise ValueError("browser image ID must be exact")
        if type(claim_timeout_seconds) is not int or not 1 <= claim_timeout_seconds <= 3600:
            raise ValueError("browser claim timeout is invalid")
        self.gateway = gateway
        self.fixtures = fixtures
        self.spool = spool
        self.image_id = image_id
        self.claim_timeout_seconds = claim_timeout_seconds

    def _row(self, project_id: UUID, run_id: UUID, job_id: UUID, *, lock: bool) -> dict[str, Any]:
        statement = (
            "SELECT * FROM browser_static_jobs WHERE project_id=:project_id AND run_id=:run_id AND id=:id"
        )
        if lock:
            statement += " FOR UPDATE"
        with self.gateway.engine.begin() as connection:
            row = (
                connection.execute(
                    text(statement),  # noqa: S608 - fixed internal lock suffix
                    {"project_id": project_id, "run_id": run_id, "id": job_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise NotFound("browser job not found")
        return dict(row)

    def launch(
        self,
        project_id: UUID,
        run_id: UUID,
        fixture_id: UUID,
        idempotency_key: UUID,
        *,
        role_profile_id: UUID | None = None,
        operator_assigned_profile_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Commit intent before making a host-visible manifest; never resend a key."""
        if not isinstance(idempotency_key, UUID) or idempotency_key.int == 0:
            raise Conflict("browser job idempotency key is invalid")
        if role_profile_id is not None and operator_assigned_profile_id is not None:
            raise Conflict("browser job actor is ambiguous")
        admitted_profile_id = role_profile_id or operator_assigned_profile_id
        actor_id = f"profile:{role_profile_id}" if role_profile_id is not None else self.gateway.actor_id
        with self.gateway.engine.connect() as connection:
            prior = (
                connection.execute(
                    text(
                        "SELECT * FROM browser_static_jobs WHERE project_id=:project_id "
                        "AND run_id=:run_id AND idempotency_key=:key"
                    ),
                    {"project_id": project_id, "run_id": run_id, "key": idempotency_key},
                )
                .mappings()
                .one_or_none()
            )
        if prior is not None:
            self._check_binding(dict(prior), fixture_id, actor_id, admitted_profile_id)
            return dict(prior)
        owner = self.fixtures.approved(project_id, run_id, fixture_id)
        job_id = uuid4()
        manifest = self.spool.plan(job_id, owner, self.image_id)
        with self.gateway.engine.begin() as connection:
            project = (
                connection.execute(
                    text("SELECT status FROM projects WHERE id=:id FOR UPDATE"),
                    {"id": project_id},
                )
                .mappings()
                .one_or_none()
            )
            if project is None or project["status"] != "active":
                raise Conflict("browser job project is not active")
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id,status FROM runs WHERE project_id=:project_id "
                        "AND id=:run_id FOR UPDATE"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None or run["status"] != "running":
                raise Conflict("browser job run is not active")
            if admitted_profile_id is not None:
                from onefoundry.role_browser_static_inspect import TOOL_ID, role_admitted
                from onefoundry.role_tool_admission import _context

                context = _context(
                    connection,
                    project_id,
                    run_id,
                    admitted_profile_id,
                    lock=True,
                    max_items=1,
                    allowed_modes=(
                        ("live_text", "synthetic_text", "manual_planning")
                        if operator_assigned_profile_id is not None
                        else ("live_text", "synthetic_text")
                    ),
                )
                if TOOL_ID not in context.snapshot_tools & context.current_tools or not role_admitted(
                    context
                ):
                    raise Conflict("browser role tool permission changed")
            previous = (
                connection.execute(
                    text(
                        "SELECT * FROM browser_static_jobs WHERE project_id=:project_id "
                        "AND run_id=:run_id AND idempotency_key=:key FOR UPDATE"
                    ),
                    {"project_id": project_id, "run_id": run_id, "key": idempotency_key},
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                self._check_binding(dict(previous), fixture_id, actor_id, admitted_profile_id)
                return dict(previous)
            # Recheck the inbox while the run is locked. The source row is
            # immutable and this decision never transitions a second time.
            approved = connection.scalar(
                text(
                    "SELECT 1 FROM browser_static_fixtures f JOIN approval_requests a "
                    "ON a.project_id=f.project_id AND a.run_id=f.run_id AND a.id=f.approval_id "
                    "WHERE f.project_id=:project_id AND f.run_id=:run_id AND f.id=:fixture_id "
                    "AND a.status='approved' AND a.proposal_digest=:digest"
                ),
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "fixture_id": fixture_id,
                    "digest": owner.proposal_digest,
                },
            )
            if approved is None:
                raise Conflict("browser fixture approval changed")
            connection.execute(
                text(
                    "INSERT INTO browser_static_jobs "
                    "(id,project_id,run_id,fixture_id,actor_id,idempotency_key,request_sha256,"
                    "binding_digest,source_sha256,image_id,status) VALUES "
                    "(:id,:project_id,:run_id,:fixture_id,:actor_id,:idempotency_key,:request_sha256,"
                    ":binding_digest,:source_sha256,:image_id,'queued')"
                ),
                {
                    "id": job_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "fixture_id": fixture_id,
                    "actor_id": actor_id,
                    "idempotency_key": idempotency_key,
                    "request_sha256": manifest["request_sha256"],
                    "binding_digest": owner.target.binding_digest,
                    "source_sha256": owner.source_sha256,
                    "image_id": self.image_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "browser_static_job_queued",
                {
                    "job_id": str(job_id),
                    "fixture_id": str(fixture_id),
                    "request_sha256": manifest["request_sha256"],
                    "binding_digest": owner.target.binding_digest,
                    "admitted_profile_id": (
                        str(admitted_profile_id) if admitted_profile_id is not None else None
                    ),
                },
                actor_id=actor_id,
            )
        try:
            staged = self.spool.stage(job_id, owner, self.image_id)
            if staged != manifest:
                raise BrowserJobError("browser_job_stage_changed")
        except (BrowserJobError, OSError) as error:
            self._terminal(project_id, run_id, job_id, "unknown", "stage_unknown", None)
            raise Conflict("browser job staging outcome is unknown") from error
        return self._row(project_id, run_id, job_id, lock=False)

    def _check_binding(
        self,
        row: dict[str, Any],
        fixture_id: UUID,
        actor_id: str,
        admitted_profile_id: UUID | None,
    ) -> None:
        if row["fixture_id"] != fixture_id or row["image_id"] != self.image_id or row["actor_id"] != actor_id:
            raise Conflict("browser job idempotency key is already bound")
        with self.gateway.engine.connect() as connection:
            event = (
                connection.execute(
                    text(
                        "SELECT payload FROM events WHERE project_id=:project_id "
                        "AND run_id=:run_id AND event_type='browser_static_job_queued' "
                        "AND payload->>'job_id'=:job_id ORDER BY created_at,id LIMIT 1"
                    ),
                    {
                        "project_id": row["project_id"],
                        "run_id": row["run_id"],
                        "job_id": str(row["id"]),
                    },
                )
                .mappings()
                .one_or_none()
            )
        expected = str(admitted_profile_id) if admitted_profile_id is not None else None
        if event is None or event["payload"].get("admitted_profile_id") != expected:
            raise Conflict("browser job role binding changed")

    def launch_for_assigned_role(
        self, project_id: UUID, run_id: UUID, fixture_id: UUID, idempotency_key: UUID
    ) -> dict[str, Any]:
        """Operator assigns one approved fixture to the run's admitted role."""
        with self.gateway.engine.connect() as connection:
            profile_id = connection.scalar(
                text("SELECT profile_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
                {"project_id": project_id, "run_id": run_id},
            )
        if profile_id is None:
            raise NotFound("browser job run not found")
        return self.launch(
            project_id,
            run_id,
            fixture_id,
            idempotency_key,
            operator_assigned_profile_id=profile_id,
        )

    def by_idempotency_key(
        self, project_id: UUID, run_id: UUID, idempotency_key: UUID
    ) -> dict[str, Any] | None:
        """Read an existing exact intent without planning or staging a new one."""
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM browser_static_jobs WHERE project_id=:project_id "
                        "AND run_id=:run_id AND idempotency_key=:key"
                    ),
                    {"project_id": project_id, "run_id": run_id, "key": idempotency_key},
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    def get(self, project_id: UUID, run_id: UUID, job_id: UUID) -> dict[str, Any]:
        """Return run-scoped job status without private inspection text or source."""
        return _metadata(self._row(project_id, run_id, job_id, lock=False))

    def list_for_run(self, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
        """Return status metadata for this exact run."""
        with self.gateway.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT * FROM browser_static_jobs WHERE project_id=:project_id "
                        "AND run_id=:run_id ORDER BY created_at,id"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .all()
            )
        return [_metadata(dict(row)) for row in rows]

    def _terminal(
        self,
        project_id: UUID,
        run_id: UUID,
        job_id: UUID,
        status: str,
        reason: str,
        receipt_sha256: str | None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"done", "failed", "unknown"} or _REASON.fullmatch(reason) is None:
            raise Conflict("browser job terminal outcome invalid")
        try:
            self.spool.purge_source(job_id)
        except (BrowserJobError, OSError) as error:
            raise Conflict("browser job source cleanup could not be confirmed") from error
        with self.gateway.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT j.*,r.work_order_id FROM browser_static_jobs j "
                        "JOIN runs r ON r.project_id=j.project_id AND r.id=j.run_id "
                        "WHERE j.project_id=:project_id AND j.run_id=:run_id AND j.id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "run_id": run_id, "id": job_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("browser job not found")
            if row["status"] != "queued":
                raise Conflict("browser job already terminal")
            connection.execute(
                text(
                    "UPDATE browser_static_jobs SET status=:status,reason_code=:reason,"
                    "receipt_sha256=:receipt,title=:title,inspection_text=:inspection_text,"
                    "blocked_request_count=:blocked_request_count,finished_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND run_id=:run_id AND id=:id AND status='queued'"
                ),
                {
                    "status": status,
                    "reason": reason,
                    "receipt": receipt_sha256,
                    "title": evidence["title"] if evidence is not None else None,
                    "inspection_text": evidence["text"] if evidence is not None else None,
                    "blocked_request_count": (
                        evidence["blocked_request_count"] if evidence is not None else None
                    ),
                    "project_id": project_id,
                    "run_id": run_id,
                    "id": job_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                run_id,
                "browser_static_job_finished",
                {
                    "job_id": str(job_id),
                    "status": status,
                    "reason_code": reason,
                    "receipt_sha256": receipt_sha256,
                },
                actor_id=row["actor_id"],
            )
        return self._row(project_id, run_id, job_id, lock=False)

    def reconcile(
        self, project_id: UUID, run_id: UUID, job_id: UUID, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Accept one matching host receipt, or report pending/unknown without retry."""
        row = self._row(project_id, run_id, job_id, lock=False)
        if row["status"] != "queued":
            return row
        observed = self.spool.read_receipt(job_id)
        if observed is None:
            claim_age = self.spool.claim_age_seconds(job_id, row["request_sha256"])
            if claim_age is not None:
                if claim_age >= self.claim_timeout_seconds:
                    return self._terminal(
                        project_id, run_id, job_id, "unknown", "claimed_receipt_missing", None
                    )
                raise Conflict("browser job claimed; receipt not yet available")
            checked_at = now or datetime.now(UTC)
            if checked_at.tzinfo is None:
                raise ValueError("browser reconciliation time must have a timezone")
            if (checked_at - row["created_at"]).total_seconds() >= self.claim_timeout_seconds:
                try:
                    fenced = self.spool.fence_unclaimed(job_id, row["request_sha256"])
                except (BrowserJobError, OSError) as error:
                    raise Conflict("browser job timeout fence could not be confirmed") from error
                if not fenced:
                    raise Conflict("browser job claimed while timeout was being reconciled")
                return self._terminal(project_id, run_id, job_id, "unknown", "host_claim_missing", None)
            raise Conflict("browser job queued; no host claim observed")
        receipt, digest = observed
        if (
            set(receipt)
            != {
                "schema_version",
                "job_id",
                "project_id",
                "run_id",
                "fixture_id",
                "request_sha256",
                "binding_digest",
                "source_sha256",
                "image_id",
                "status",
                "reason_code",
                "title",
                "text",
                "blocked_request_count",
                "finished_at",
            }
            or receipt["schema_version"] != 1
            or receipt["job_id"] != str(job_id)
            or receipt["project_id"] != str(project_id)
            or receipt["run_id"] != str(run_id)
            or receipt["fixture_id"] != str(row["fixture_id"])
            or receipt["request_sha256"] != row["request_sha256"]
            or receipt["binding_digest"] != row["binding_digest"]
            or receipt["source_sha256"] != row["source_sha256"]
            or receipt["image_id"] != row["image_id"]
            or receipt["status"] not in {"done", "unknown"}
            or not isinstance(receipt["reason_code"], str)
            or _REASON.fullmatch(receipt["reason_code"]) is None
            or (receipt["status"] == "done" and receipt["reason_code"] != "inspection_complete")
            or (receipt["status"] == "unknown" and receipt["reason_code"] != "runner_failure_after_claim")
            or not self.spool.claimed(job_id, row["request_sha256"])
        ):
            raise Conflict("browser job receipt does not match committed intent")
        try:
            completed = datetime.fromisoformat(receipt["finished_at"])
        except (TypeError, ValueError) as error:
            raise Conflict("browser job receipt time invalid") from error
        if completed.tzinfo is None:
            raise Conflict("browser job receipt time invalid")
        if receipt["status"] == "done":
            if (
                not isinstance(receipt["title"], str)
                or not isinstance(receipt["text"], str)
                or len(receipt["title"]) > 128
                or len(receipt["text"]) > 4096
                or contains_sensitive_text(receipt["title"])
                or contains_sensitive_text(receipt["text"])
                or type(receipt["blocked_request_count"]) is not int
                or not 0 <= receipt["blocked_request_count"] <= 1000
            ):
                raise Conflict("browser job receipt text invalid")
        elif any(receipt[key] is not None for key in ("title", "text", "blocked_request_count")):
            raise Conflict("browser job unknown receipt contains result")
        return self._terminal(
            project_id,
            run_id,
            job_id,
            receipt["status"],
            receipt["reason_code"],
            digest,
            receipt if receipt["status"] == "done" else None,
        )

    def evidence(self, project_id: UUID, run_id: UUID, job_id: UUID) -> dict[str, Any]:
        """Private project/run-scoped readback of a terminal inspection."""
        row = self._row(project_id, run_id, job_id, lock=False)
        if row["status"] != "done":
            raise Conflict("browser inspection evidence is not available")
        return {
            "job_id": row["id"],
            "project_id": row["project_id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "title": row["title"],
            "text": row["inspection_text"],
            "blocked_request_count": row["blocked_request_count"],
            "receipt_sha256": row["receipt_sha256"],
            "finished_at": row["finished_at"],
        }
