"""Atomic workflow admission and owner-observed settlement."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound

if TYPE_CHECKING:
    from onefoundry.settings import Settings

_LIMITS = {
    "linked_order": "linked_orders_limit",
    "model_attempt": "model_attempts_limit",
    "public_web_call": "public_web_calls_limit",
}


def context_limit(settings: "Settings", policy: Mapping[str, Any]) -> int | None:
    """Disable only prompt-size admission; preserve immutable historical budget snapshots."""
    if settings.onefoundry_max_context_bytes is None:
        return None
    saved = policy["context_bytes_limit"]
    return int(saved) if saved is not None else settings.onefoundry_max_context_bytes


def mark_block(connection: Any, gateway: Any, project_id: UUID, workflow_id: UUID, reason: str) -> None:
    if reason not in {"budget_exceeded", "usage_unknown"}:
        raise ValueError("invalid workflow block reason")
    current = (
        connection.execute(
            text(
                "SELECT status,blocked_reason FROM onefoundry_workflows "
                "WHERE project_id=:p AND id=:w FOR UPDATE"
            ),
            {"p": project_id, "w": workflow_id},
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        raise NotFound("workflow not found")
    target_status = "unknown" if reason == "usage_unknown" else "blocked"
    if current["status"] == target_status and current["blocked_reason"] == reason:
        return
    connection.execute(
        text(
            "UPDATE onefoundry_workflows SET status=:status,blocked_reason=:reason,"
            "revision=revision+1 WHERE project_id=:p AND id=:w"
        ),
        {"status": target_status, "reason": reason, "p": project_id, "w": workflow_id},
    )
    gateway._event(
        connection,
        project_id,
        None,
        None,
        "onefoundry_workflow_blocked",
        {"workflow_id": str(workflow_id), "reason": reason},
    )


def record_denial(
    gateway: Any, project_id: UUID, organization_id: UUID | None, order_id: UUID | None, message: str
) -> None:
    reason = (
        "budget_exceeded"
        if message.startswith("budget_exceeded")
        else ("usage_unknown" if message.startswith("usage_unknown") else None)
    )
    if reason is None:
        return
    with gateway.engine.begin() as connection:
        if organization_id is not None:
            workflow_id = connection.scalar(
                text("SELECT id FROM onefoundry_workflows WHERE project_id=:p AND organization_id=:o"),
                {"p": project_id, "o": organization_id},
            )
        elif order_id is not None:
            workflow_id = linked_workflow(connection, project_id, order_id)
        else:
            return
        if workflow_id is not None:
            mark_block(connection, gateway, project_id, workflow_id, reason)


def snapshot(connection: Any, gateway: Any, project_id: UUID, workflow_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text("SELECT * FROM onefoundry_budget_snapshots WHERE project_id=:p AND workflow_id=:w"),
            {"p": project_id, "w": workflow_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is not None:
        return dict(row)
    settings = gateway.live_text_settings
    if settings is None:
        raise Conflict("OneFoundry budget settings are unavailable")
    connection.execute(
        text(
            "INSERT INTO onefoundry_budget_snapshots "
            "(id,project_id,actor_id,workflow_id,linked_orders_limit,model_attempts_limit,"
            "public_web_calls_limit,context_bytes_limit,output_tokens_limit,elapsed_seconds_limit) "
            "VALUES (:id,:p,:actor,:w,:orders,:attempts,:web,:context,:output,:elapsed) "
            "ON CONFLICT (project_id,workflow_id) DO NOTHING"
        ),
        {
            "id": uuid4(),
            "p": project_id,
            "actor": gateway.actor_id,
            "w": workflow_id,
            "orders": settings.onefoundry_max_linked_orders,
            "attempts": settings.onefoundry_max_model_attempts,
            "web": settings.onefoundry_max_public_web_calls,
            "context": settings.onefoundry_max_context_bytes,
            "output": settings.onefoundry_max_output_tokens,
            "elapsed": settings.onefoundry_max_elapsed_seconds,
        },
    )
    return dict(
        connection.execute(
            text("SELECT * FROM onefoundry_budget_snapshots WHERE project_id=:p AND workflow_id=:w"),
            {"p": project_id, "w": workflow_id},
        )
        .mappings()
        .one()
    )


def linked_workflow(connection: Any, project_id: UUID, order_id: UUID) -> UUID | None:
    return cast(
        UUID | None,
        connection.scalar(
            text(
                "SELECT workflow_id FROM onefoundry_work_order_subjects "
                "WHERE project_id=:p AND work_order_id=:o"
            ),
            {"p": project_id, "o": order_id},
        ),
    )


def reserve(
    connection: Any,
    gateway: Any,
    project_id: UUID,
    workflow_id: UUID,
    operation_key: str,
    kind: str,
) -> dict[str, Any]:
    if kind not in _LIMITS or not operation_key or len(operation_key) > 160:
        raise ValueError("invalid budget operation")
    workflow = (
        connection.execute(
            text("SELECT created_at FROM onefoundry_workflows WHERE project_id=:p AND id=:w FOR UPDATE"),
            {"p": project_id, "w": workflow_id},
        )
        .mappings()
        .one_or_none()
    )
    if workflow is None:
        raise NotFound("workflow not found")
    policy = snapshot(connection, gateway, project_id, workflow_id)
    existing = (
        connection.execute(
            text(
                "SELECT * FROM onefoundry_budget_reservations WHERE project_id=:p AND workflow_id=:w "
                "AND operation_key=:key"
            ),
            {"p": project_id, "w": workflow_id, "key": operation_key},
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["kind"] != kind:
            raise Conflict("budget operation key belongs to another kind")
        return dict(existing)
    elapsed = connection.scalar(
        text("SELECT EXTRACT(EPOCH FROM (clock_timestamp()-:started))"), {"started": policy["created_at"]}
    )
    if elapsed is None or elapsed >= policy["elapsed_seconds_limit"]:
        raise Conflict("budget_exceeded: workflow elapsed limit")
    unknown = connection.scalar(
        text(
            "SELECT 1 FROM onefoundry_budget_reservations WHERE project_id=:p AND workflow_id=:w "
            "AND state='unknown' LIMIT 1"
        ),
        {"p": project_id, "w": workflow_id},
    )
    if unknown is not None:
        raise Conflict("usage_unknown: reconcile prior attempt")
    count = connection.scalar(
        text(
            "SELECT count(*) FROM onefoundry_budget_reservations WHERE project_id=:p AND workflow_id=:w "
            "AND kind=:kind"
        ),
        {"p": project_id, "w": workflow_id, "kind": kind},
    )
    if count >= policy[_LIMITS[kind]]:
        raise Conflict("budget_exceeded: workflow operation limit")
    reservation_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO onefoundry_budget_reservations "
            "(id,project_id,actor_id,workflow_id,operation_key,kind) "
            "VALUES (:id,:p,:actor,:w,:key,:kind)"
        ),
        {
            "id": reservation_id,
            "p": project_id,
            "actor": gateway.actor_id,
            "w": workflow_id,
            "key": operation_key,
            "kind": kind,
        },
    )
    gateway._event(
        connection,
        project_id,
        None,
        None,
        "onefoundry_budget_reserved",
        {"reservation_id": str(reservation_id), "workflow_id": str(workflow_id), "kind": kind},
    )
    return dict(
        connection.execute(
            text("SELECT * FROM onefoundry_budget_reservations WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": reservation_id},
        )
        .mappings()
        .one()
    )


def settle(
    connection: Any,
    gateway: Any,
    project_id: UUID,
    reservation_id: UUID,
    owner_id: UUID,
    state: str,
    tokens: int | None,
    cost_usd: float | None,
) -> dict[str, Any]:
    current = (
        connection.execute(
            text("SELECT * FROM onefoundry_budget_reservations WHERE project_id=:p AND id=:id FOR UPDATE"),
            {"p": project_id, "id": reservation_id},
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        raise NotFound("budget reservation not found")
    if current["state"] != "reserved":
        return dict(current)
    if state not in {"settled", "unknown"}:
        raise ValueError("invalid settlement")
    work_order_id = None
    run_id = None
    if current["kind"] in {"model_attempt", "public_web_call"}:
        work_order_id = connection.scalar(
            text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": owner_id},
        )
        if work_order_id is None:
            raise NotFound("run owner not found for budget settlement")
        run_id = owner_id
    else:
        work_order_id = connection.scalar(
            text("SELECT id FROM work_orders WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": owner_id},
        )
        if work_order_id is None:
            raise NotFound("work order owner not found for budget settlement")
    linked_owner = connection.scalar(
        text(
            "SELECT 1 FROM onefoundry_work_order_subjects WHERE project_id=:p "
            "AND workflow_id=:w AND work_order_id=:order"
        ),
        {"p": project_id, "w": current["workflow_id"], "order": work_order_id},
    )
    if linked_owner is None:
        raise Conflict("budget settlement owner is not linked to this workflow")
    connection.execute(
        text(
            "UPDATE onefoundry_budget_reservations SET state=:state,owner_id=:owner,"
            "observed_tokens=:tokens,observed_cost_usd=:cost WHERE project_id=:p AND id=:id"
        ),
        {
            "state": state,
            "owner": owner_id,
            "tokens": tokens,
            "cost": cost_usd,
            "p": project_id,
            "id": reservation_id,
        },
    )
    gateway._event(
        connection,
        project_id,
        work_order_id,
        run_id,
        "onefoundry_budget_settled",
        {"reservation_id": str(reservation_id), "state": state},
    )
    if state == "unknown":
        mark_block(connection, gateway, project_id, current["workflow_id"], "usage_unknown")
    return dict(
        connection.execute(
            text("SELECT * FROM onefoundry_budget_reservations WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": reservation_id},
        )
        .mappings()
        .one()
    )
