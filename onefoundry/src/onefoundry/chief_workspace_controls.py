"""Reversible visibility and future-turn pause, separate from work outcomes."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.chief_workspace_schemas import ChiefControl
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def require_operator(gateway: "ToolGateway") -> None:
    settings = gateway.live_text_settings
    if settings is None or gateway.actor_id != settings.operator_actor_id:
        raise Conflict("only the operator may direct Chief conversations")


def root_order(connection: Any, project_id: UUID, order_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text("SELECT * FROM work_orders WHERE project_id=:p AND id=:o AND parent_work_order_id IS NULL"),
            {"p": project_id, "o": order_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("root conversation not found in this project")
    return dict(row)


def control(gateway: "ToolGateway", project_id: UUID, order_id: UUID, data: ChiefControl) -> dict[str, Any]:
    require_operator(gateway)
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='chief_conversation_control' "
                "AND payload->>'client_action_id'=:client LIMIT 1"
            ),
            {"p": project_id, "client": str(data.client_action_id)},
        )
        request = data.model_dump(mode="json")
        if prior is not None:
            if prior["order_id"] != str(order_id) or prior["request"] != request:
                raise Conflict("control identity belongs to another request")
            return dict(prior["result"])
        order = root_order(connection, project_id, order_id)
        row = (
            connection.execute(
                text("SELECT * FROM chief_conversation_controls WHERE project_id=:p AND work_order_id=:o"),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        version = row["version"] if row is not None else 0
        if order["version"] != data.expected_version or version != data.expected_controls_version:
            raise Conflict("conversation changed; refresh before applying this action")
        if row is not None and row["trashed"] and data.action != "restore":
            raise Conflict("restore this conversation from Trash before changing it")
        if row is not None and row["archived"] and data.action == "resume":
            raise Conflict("restore this conversation from Archived before resuming it")
        if data.action == "trash":
            if row is None or not row["archived"]:
                raise Conflict("archive the conversation before moving it to Trash")
            if connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM task_session_attempts WHERE project_id=:p "
                    "AND root_work_order_id=:o AND status='running')"
                ),
                {"p": project_id, "o": order_id},
            ):
                raise Conflict("the current turn must finish before moving this conversation to Trash")
        connection.execute(
            text(
                "INSERT INTO chief_conversation_controls "
                "(project_id,work_order_id,actor_id) VALUES (:p,:o,:actor) "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": project_id, "o": order_id, "actor": gateway.actor_id},
        )
        if data.action == "archive":
            connection.execute(
                text(
                    "UPDATE chief_conversation_controls SET archived=true,paused=true "
                    "WHERE project_id=:p AND work_order_id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
        elif data.action == "trash":
            connection.execute(
                text(
                    "UPDATE chief_conversation_controls SET trashed=true,archived=true,paused=true "
                    "WHERE project_id=:p AND work_order_id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
        elif data.action == "restore" and row is not None and row["trashed"]:
            connection.execute(
                text(
                    "UPDATE chief_conversation_controls SET trashed=false "
                    "WHERE project_id=:p AND work_order_id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
        elif data.action == "rename":
            connection.execute(
                text("UPDATE work_orders SET title=:title WHERE project_id=:p AND id=:o"),
                {"p": project_id, "o": order_id, "title": data.title},
            )
        else:
            statement = (
                "UPDATE chief_conversation_controls SET paused=:value WHERE "
                "project_id=:p AND work_order_id=:o"
                if data.action in ("pause", "resume")
                else "UPDATE chief_conversation_controls SET archived=:value WHERE "
                "project_id=:p AND work_order_id=:o"
            )
            connection.execute(
                text(statement),
                {"p": project_id, "o": order_id, "value": data.action in ("pause", "archive")},
            )
        if version:
            connection.execute(
                text(
                    "UPDATE chief_conversation_controls SET version=version+1 WHERE "
                    "project_id=:p AND work_order_id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
        result = {
            "order_id": str(order_id),
            "action": data.action,
            "controls_version": version + 1,
            "version": order["version"] + (data.action == "rename"),
        }
        gateway._event(
            connection,
            project_id,
            order_id,
            None,
            "chief_conversation_control",
            {
                "order_id": str(order_id),
                "client_action_id": str(data.client_action_id),
                "request": request,
                "result": result,
            },
        )
        return result
