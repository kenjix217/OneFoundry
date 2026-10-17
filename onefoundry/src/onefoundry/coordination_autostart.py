"""Start queued rule and Chief assignments through the normal live-text run path.

Rules or Chief decisions may queue a launch only while ``COORDINATION_MODE=auto``.
Each launch makes at most one run attempt, counts
toward a per-project daily cap, and is never retried when its outcome is
uncertain. Completion still follows the deliverable and review rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.coordination_rule_limits import quiet_until
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway


def _finish(
    gateway: ToolGateway, launch: dict[str, Any], state: str, error: str | None, detail: dict[str, Any] | None
) -> None:
    with gateway.engine.begin() as connection:
        changed = connection.execute(
            text(
                "UPDATE coordination_intents SET state=:s,error_code=:e,decision=:d,lease_until=NULL,"
                "updated_at=clock_timestamp() WHERE project_id=:p AND id=:id AND state='leased'"
            ).bindparams(bindparam("d", type_=JSONB(none_as_null=True))),
            {"s": state, "e": error, "d": detail, "p": launch["project_id"], "id": launch["id"]},
        )
        if changed.rowcount != 1:
            return
        gateway._event(
            connection,
            launch["project_id"],
            launch["work_order_id"],
            None,
            f"coordination_launch_{state}",
            {"intent_id": str(launch["id"]), "error_code": error, **(detail or {})},
        )


def claim(gateway: ToolGateway, settings: Settings, project_id: UUID | None = None) -> dict[str, Any] | None:
    """Expire interrupted launches as unknown, then lease one pending launch or block it."""
    with gateway.engine.begin() as connection:
        for expired in connection.execute(
            text(
                "UPDATE coordination_intents SET state='unknown',error_code='launch_interrupted',"
                "lease_until=NULL,updated_at=clock_timestamp() "
                "WHERE action_kind='auto_start' AND state='leased' "
                "AND lease_until<clock_timestamp() RETURNING id,project_id,work_order_id"
            )
        ).mappings():
            gateway._event(
                connection,
                expired["project_id"],
                expired["work_order_id"],
                None,
                "coordination_launch_unknown",
                {"intent_id": str(expired["id"]), "error_code": "launch_interrupted"},
            )
        project_filter = "i.project_id=:project_id AND " if project_id is not None else ""
        launch = (
            connection.execute(
                text(
                    "SELECT i.id,i.project_id,i.work_order_id,i.rule_id,o.status AS order_status,"  # noqa: S608
                    "p.status AS project_status,v.max_model_attempts,v.quiet_hours "
                    "FROM coordination_intents i JOIN work_orders o ON o.project_id=i.project_id "
                    "AND o.id=i.work_order_id JOIN projects p ON p.id=i.project_id "
                    "LEFT JOIN coordination_rule_versions v ON v.project_id=i.project_id "
                    "AND v.rule_id=i.rule_id AND v.version=i.rule_version WHERE "
                    + project_filter
                    + "i.action_kind='auto_start' AND i.state='pending' "
                    "AND i.due_at<=clock_timestamp() AND NOT EXISTS("
                    "SELECT 1 FROM coordination_controls c WHERE c.project_id=i.project_id AND c.paused) "
                    "ORDER BY i.created_at,i.id FOR UPDATE OF i SKIP LOCKED LIMIT 1"
                ),
                {"project_id": project_id} if project_id is not None else {},
            )
            .mappings()
            .one_or_none()
        )
        if launch is None:
            return None
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"coordination-launch:{launch['project_id']}"},
        )
        now = connection.scalar(text("SELECT clock_timestamp()"))
        deferred = quiet_until(launch["quiet_hours"], now, settings.coordination_timezone)
        if deferred is not None:
            connection.execute(
                text("UPDATE coordination_intents SET due_at=:due WHERE project_id=:p AND id=:id"),
                {"due": deferred, "p": launch["project_id"], "id": launch["id"]},
            )
            gateway._event(
                connection,
                launch["project_id"],
                launch["work_order_id"],
                None,
                "coordination_launch_deferred",
                {
                    "intent_id": str(launch["id"]),
                    "reason": "rule_quiet_hours",
                    "due_at": deferred.isoformat(),
                },
            )
            return {}
        started_today = connection.scalar(
            text(
                "SELECT count(*) FROM coordination_intents WHERE project_id=:p "
                "AND action_kind='auto_start' AND model_admitted_at >= "
                "(date_trunc('day',clock_timestamp() AT TIME ZONE :zone) AT TIME ZONE :zone)"
            ),
            {"p": launch["project_id"], "zone": settings.coordination_timezone},
        )
        rule_today = connection.scalar(
            text(
                "SELECT count(*) FROM coordination_intents WHERE project_id=:p AND rule_id=:rule "
                "AND action_kind='auto_start' AND model_admitted_at >= "
                "(date_trunc('day',clock_timestamp() AT TIME ZONE :zone) AT TIME ZONE :zone)"
            ),
            {"p": launch["project_id"], "rule": launch["rule_id"], "zone": settings.coordination_timezone},
        )
        error = (
            "auto_mode_off"
            if settings.coordination_mode != "auto"
            else "project_inactive"
            if launch["project_status"] != "active"
            else "work_order_changed"
            if launch["order_status"] != "ready"
            else "auto_start_daily_limit"
            if int(started_today) >= settings.coordination_max_auto_starts_per_day
            else "rule_model_attempt_limit"
            if launch["max_model_attempts"] is not None and int(rule_today) >= launch["max_model_attempts"]
            else None
        )
        connection.execute(
            text(
                "UPDATE coordination_intents SET state=:s,error_code=:e,attempts=attempts+1,"
                "lease_until=CASE WHEN :s='leased' "
                "THEN clock_timestamp()+(:seconds * interval '1 second') END,"
                "model_admitted_at=CASE WHEN :s='leased' THEN clock_timestamp() END,"
                "updated_at=clock_timestamp() WHERE project_id=:p AND id=:id"
            ),
            {
                "s": "blocked" if error else "leased",
                "e": error,
                "seconds": settings.live_text_timeout_seconds + 120,
                "p": launch["project_id"],
                "id": launch["id"],
            },
        )
        if error:
            gateway._event(
                connection,
                launch["project_id"],
                launch["work_order_id"],
                None,
                "coordination_launch_blocked",
                {"intent_id": str(launch["id"]), "error_code": error},
            )
            return {}
        return dict(launch)


def step(gateway: ToolGateway, settings: Settings, project_id: UUID | None = None) -> bool:
    """Attempt one launch. Returns whether a launch was claimed or blocked."""
    from onefoundry.live_text_store import LiveTextFailure, LiveTextUnknown

    launch = claim(gateway, settings, project_id)
    if launch is None:
        return False
    if not launch:
        return True
    try:
        result = gateway.run_live_text(launch["project_id"], launch["work_order_id"])
    except LiveTextFailure as failure:
        _finish(gateway, launch, "blocked", "auto_start_failed", {"run_id": str(failure.run_id)})
    except LiveTextUnknown as failure:
        _finish(gateway, launch, "unknown", "auto_start_outcome_unknown", {"run_id": str(failure.run_id)})
    except (Conflict, NotFound, Unavailable) as failure:
        _finish(gateway, launch, "blocked", "auto_start_unavailable", {"reason": str(failure)[:300]})
    except Exception:
        _finish(gateway, launch, "unknown", "auto_start_outcome_unknown", None)
    else:
        run = result.get("run") or {}
        _finish(
            gateway, launch, "done", None, {"run_id": str(run.get("id", "")), "run_status": run.get("status")}
        )
    return True
