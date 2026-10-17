"""Coordinator service: built-in business continuation and operator coordination rules.

It holds database access only, never calls a model, and never starts a run.
"""

import signal
from threading import Event
from typing import Any, cast
from uuid import UUID

from sqlalchemy import create_engine, text

from onefoundry import coordination_store
from onefoundry.coordination_store import NOT_PAUSED
from onefoundry.gateway_errors import Conflict
from onefoundry.onefoundry_workflow import LinkedOrderRequest
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway


def claim_due(
    gateway: ToolGateway, settings: Settings, project_id: UUID | None = None
) -> dict[str, Any] | None:
    with gateway.engine.begin() as connection:
        project_filter = "project_id=:project_id AND " if project_id is not None else ""
        row = (
            connection.execute(
                text(
                    "SELECT id,project_id,organization_id,workflow_id,transition_revision,attempts "
                    "FROM coordination_intents i WHERE "
                    + project_filter
                    + "action_kind='onefoundry_draft_outreach' AND "
                    "((state='pending' AND due_at<=clock_timestamp()) OR "
                    "(state='leased' AND lease_until<=clock_timestamp())) AND "
                    + NOT_PAUSED.format(alias="i")
                    + " "
                    "ORDER BY due_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"
                ),
                {"project_id": project_id} if project_id is not None else {},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["attempts"] >= settings.onefoundry_coordinator_max_attempts:
            connection.execute(
                text(
                    "UPDATE coordination_intents SET state='blocked',error_code='attempt_limit',"
                    "lease_until=NULL,updated_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": row["id"]},
            )
            return None
        leased = (
            connection.execute(
                text(
                    "UPDATE coordination_intents SET state='leased',attempts=attempts+1,"
                    "lease_until=clock_timestamp()+(:seconds * interval '1 second'),"
                    "updated_at=clock_timestamp() WHERE id=:id "
                    "RETURNING id,project_id,organization_id,workflow_id,transition_revision,attempts"
                ),
                {"id": row["id"], "seconds": settings.onefoundry_coordinator_lease_seconds},
            )
            .mappings()
            .one()
        )
        return dict(leased)


def _existing_order(connection: Any, project_id: UUID, workflow_id: UUID) -> UUID | None:
    return cast(
        UUID | None,
        connection.scalar(
            text(
                "SELECT work_order_id FROM onefoundry_work_order_subjects "
                "WHERE project_id=:p AND workflow_id=:w AND step_key='draft' "
                "ORDER BY created_at,id LIMIT 1"
            ),
            {"p": project_id, "w": workflow_id},
        ),
    )


def _ready_state(gateway: ToolGateway, intent: dict[str, Any]) -> tuple[UUID | None, int | None, str | None]:
    with gateway.engine.connect() as connection:
        project_id = intent["project_id"]
        workflow_id = intent["workflow_id"]
        existing = _existing_order(connection, project_id, workflow_id)
        if existing is not None:
            return existing, None, None
        row = (
            connection.execute(
                text(
                    "SELECT p.status AS project_status,o.qualification,w.status AS workflow_status,"
                    "w.revision FROM coordination_intents i "
                    "JOIN projects p ON p.id=i.project_id "
                    "JOIN onefoundry_organizations o ON o.project_id=i.project_id AND o.id=i.organization_id "
                    "JOIN onefoundry_workflows w ON w.project_id=i.project_id AND w.id=i.workflow_id "
                    "WHERE i.project_id=:p AND i.id=:id"
                ),
                {"p": project_id, "id": intent["id"]},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["project_status"] != "active":
            return None, None, "project_unavailable"
        if row["qualification"] != "qualified" or row["workflow_status"] != "research_reviewed":
            return None, None, "state_changed"
        if row["revision"] != intent["transition_revision"]:
            return None, None, "workflow_changed"
        approved = connection.scalar(
            text(
                "SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
                "ON s.project_id=c.project_id AND s.id=c.source_id "
                "WHERE c.project_id=:p AND c.organization_id=:o AND c.review_state='approved' "
                "AND c.superseded_by IS NULL AND s.superseded_by IS NULL "
                "AND c.source_revision=s.revision LIMIT 1"
            ),
            {"p": project_id, "o": intent["organization_id"]},
        )
        if approved is None:
            return None, None, "evidence_stale"
        profile_id = connection.scalar(
            text(
                "SELECT id FROM profiles WHERE project_id=:p AND kind='builtin' AND name='Sales' "
                "ORDER BY created_at,id LIMIT 1"
            ),
            {"p": project_id},
        )
        if profile_id is None:
            return None, None, "sales_profile_missing"
        return None, row["revision"], str(profile_id)


def _finish(
    gateway: ToolGateway,
    intent: dict[str, Any],
    state: str,
    order_id: UUID | None,
    error_code: str | None,
) -> bool:
    with gateway.engine.begin() as connection:
        changed = connection.execute(
            text(
                "UPDATE coordination_intents SET state=:state,work_order_id=:order_id,"
                "error_code=:error_code,lease_until=NULL,updated_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id AND state='leased' AND attempts=:attempts"
            ),
            {
                "state": state,
                "order_id": order_id,
                "error_code": error_code,
                "p": intent["project_id"],
                "id": intent["id"],
                "attempts": intent["attempts"],
            },
        )
        return changed.rowcount == 1


def run_once(gateway: ToolGateway, settings: Settings, project_id: UUID | None = None) -> bool:
    if settings.onefoundry_business_mode not in {"event", "inbox"}:
        raise ValueError("OneFoundry coordinator requires event mode")
    intent = claim_due(gateway, settings, project_id)
    if intent is None:
        return False
    order_id, revision, profile_or_error = _ready_state(gateway, intent)
    if order_id is not None:
        _finish(gateway, intent, "done", order_id, None)
        return True
    if revision is None or profile_or_error is None:
        _finish(gateway, intent, "blocked", None, profile_or_error or "state_unavailable")
        return True
    try:
        gateway.onefoundry_linked_order(
            intent["project_id"],
            intent["organization_id"],
            "draft",
            LinkedOrderRequest(
                profile_id=UUID(profile_or_error),
                idempotency_key=intent["id"],
                expected_revision=revision,
            ),
        )
    except Conflict as error:
        with gateway.engine.connect() as connection:
            order_id = _existing_order(connection, intent["project_id"], intent["workflow_id"])
        if order_id is None:
            code = "budget_exceeded" if str(error).startswith("budget_exceeded") else "admission_changed"
            _finish(gateway, intent, "blocked", None, code)
            return True
    except Exception:
        # A failed owner readback is uncertain. The lease remains available for
        # bounded recovery with the same idempotency key after expiry.
        return True
    with gateway.engine.connect() as connection:
        order_id = _existing_order(connection, intent["project_id"], intent["workflow_id"])
    if order_id is None:
        _finish(gateway, intent, "unknown", None, "owner_readback_missing")
    else:
        _finish(gateway, intent, "done", order_id, None)
    return True


def main() -> None:
    settings = Settings()
    business = settings.onefoundry_business_mode in {"event", "inbox"}
    rules = settings.coordination_mode != "off"
    if not business and not rules:
        return  # Nothing is enabled; exit cleanly so Compose does not restart it.
    gateway = ToolGateway(
        create_engine(settings.database_url),
        settings.onefoundry_coordinator_actor_id,
        live_text_settings=settings,
    )
    stopping = Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set():
            progressed = False
            if business:
                progressed = run_once(gateway, settings) or progressed
            if rules:
                progressed = coordination_store.run_once(gateway, settings) or progressed
            if not progressed:
                stopping.wait(settings.onefoundry_coordinator_poll_seconds)
    finally:
        gateway.engine.dispose()


if __name__ == "__main__":
    main()
