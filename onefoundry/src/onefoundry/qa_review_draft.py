"""Turn one saved role QA request into an operator-authored QA draft."""

# Interpolated SQL fragment below is the fixed WORK_ORDER_COLUMNS module constant.
# ruff: noqa: S608

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.tool_gateway import WORK_ORDER_COLUMNS
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class QAReviewDraftCreate(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=16000)

    @field_validator("title", "instructions")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("QA draft text appears to contain a secret")
        return value


def _request(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    request_event_id: UUID,
    *,
    lock_run: bool,
) -> dict[str, Any]:
    lock = " FOR UPDATE" if lock_run else ""
    run = (
        connection.execute(
            text("SELECT work_order_id FROM runs WHERE project_id=:project_id AND id=:run_id" + lock),
            {"project_id": project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if run is None:
        raise NotFound("run not found")
    event = (
        connection.execute(
            text(
                "SELECT actor_id,payload FROM events WHERE project_id=:project_id "
                "AND run_id=:run_id AND work_order_id=:work_order_id "
                "AND id=:event_id AND event_type='qa_review_requested'"
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "work_order_id": run["work_order_id"],
                "event_id": request_event_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if event is None:
        raise NotFound("QA review request not found for this run")
    return dict(event)


def _linked_draft(connection: Connection, project_id: UUID, request_event_id: UUID) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                f"SELECT {WORK_ORDER_COLUMNS} FROM work_orders WHERE project_id=:project_id "
                "AND id=(SELECT work_order_id FROM events WHERE project_id=:project_id "
                "AND event_type='work_order_created' "
                "AND payload->>'source_qa_request_event_id'=:request_event_id "
                "ORDER BY created_at,id LIMIT 1)"
            ),
            {"project_id": project_id, "request_event_id": str(request_event_id)},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def read_from_request(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, request_event_id: UUID
) -> dict[str, Any] | None:
    """Read the exact linked draft, if one exists, without modifying owner state."""
    with gateway.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        _request(connection, project_id, run_id, request_event_id, lock_run=False)
        return _linked_draft(connection, project_id, request_event_id)


def create_from_request(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    request_event_id: UUID,
    data: QAReviewDraftCreate,
) -> dict[str, Any]:
    """Serialize by source run, then save or read back the one linked draft."""
    with gateway.engine.begin() as connection:
        project_status = connection.scalar(
            text("SELECT status FROM projects WHERE id=:project_id FOR UPDATE"),
            {"project_id": project_id},
        )
        if project_status is None:
            raise NotFound("project not found")
        if project_status != "active":
            raise Conflict("project is not active")
        request = _request(connection, project_id, run_id, request_event_id, lock_run=True)
        linked = _linked_draft(connection, project_id, request_event_id)
        if linked is not None:
            if linked["title"] != data.title or linked["instructions"] != data.instructions:
                raise Conflict("QA request already has a draft with different content")
            return dict(linked)

        target_text = request["payload"].get("target_profile_id")
        if not isinstance(target_text, str) or not request["actor_id"].startswith("profile:"):
            raise Conflict("QA request owner record is invalid")
        try:
            target_id = UUID(target_text)
        except ValueError:
            raise Conflict("QA request target is invalid") from None
        target = (
            connection.execute(
                text(
                    "SELECT name,kind FROM profiles WHERE project_id=:project_id AND id=:profile_id "
                    "FOR KEY SHARE"
                ),
                {"project_id": project_id, "profile_id": target_id},
            )
            .mappings()
            .one_or_none()
        )
        if target is None or target["name"] != "QA" or target["kind"] != "builtin":
            raise Conflict("QA request target is no longer this project's built-in QA profile")
        order_id = uuid4()
        saved = (
            connection.execute(
                text(
                    "INSERT INTO work_orders (id,project_id,actor_id,title,instructions,profile_id,"
                    "parent_work_order_id,source_run_id,status) "
                    "VALUES (:id,:project_id,:actor_id,:title,:instructions,:profile_id,"
                    "(SELECT work_order_id FROM runs WHERE project_id=:project_id AND id=:source),"
                    ":source,'draft') "
                    f"RETURNING {WORK_ORDER_COLUMNS}"
                ),
                {
                    "id": order_id,
                    "project_id": project_id,
                    "actor_id": gateway.actor_id,
                    "title": data.title,
                    "instructions": data.instructions,
                    "profile_id": target_id,
                    "source": run_id,
                },
            )
            .mappings()
            .one()
        )
        gateway._event(
            connection,
            project_id,
            order_id,
            None,
            "work_order_created",
            {
                "status": "draft",
                "profile_id": str(target_id),
                "source_run_id": str(run_id),
                "source_qa_request_event_id": str(request_event_id),
            },
        )
        readback = (
            connection.execute(
                text(f"SELECT {WORK_ORDER_COLUMNS} FROM work_orders WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": order_id},
            )
            .mappings()
            .one()
        )
        if (
            readback["id"] != saved["id"]
            or readback["profile_id"] != target_id
            or readback["status"] != "draft"
            or readback["title"] != data.title
            or readback["instructions"] != data.instructions
        ):
            raise Conflict("QA draft owner readback did not match")
        return dict(readback)
