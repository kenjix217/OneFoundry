"""One operator start: admit, activate, save the message and queue atomically."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.operator_message_contract import MAX_OPERATOR_MESSAGE_CHARS
from onefoundry.profile_hierarchy import lock_project
from onefoundry.task_session_admission import TaskSessionAdmission
from onefoundry.task_session_schemas import SessionMessageCreate
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.work_order_readiness import mark_ready
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class TeamStart(StrictModel):
    client_message_id: UUID
    expected_version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=MAX_OPERATOR_MESSAGE_CHARS)

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        return local_text(value)


class TaskSessionStart:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def _lookup(self, connection: Any, project_id: UUID, order_id: UUID, client_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT m.id AS message_id,m.session_id,m.sequence,m.body,a.id AS attempt_id "
                    "FROM task_session_messages m JOIN task_sessions s "
                    "ON s.project_id=m.project_id AND s.id=m.session_id "
                    "JOIN events e ON e.project_id=m.project_id AND e.work_order_id=:o "
                    "AND e.event_type='task_session_work_started' "
                    "AND e.payload->>'message_id'=m.id::text "
                    "LEFT JOIN task_session_attempts a ON a.project_id=m.project_id "
                    "AND a.source_message_id=m.id "
                    "WHERE m.project_id=:p AND m.client_message_id=:client "
                    "AND s.work_order_id=:o AND s.is_order_owner"
                ),
                {"p": project_id, "o": order_id, "client": client_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("no saved start exists for this request")
        return {**dict(row), "state": "queued"}

    def lookup(self, project_id: UUID, order_id: UUID, client_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            saved = self._lookup(connection, project_id, order_id, client_id)
            saved.pop("body")
            return saved

    def start(self, project_id: UUID, order_id: UUID, data: TeamStart) -> tuple[dict[str, Any], bool]:
        with self.gateway.engine.begin() as connection:
            return self.start_in_transaction(connection, project_id, order_id, data)

    def start_in_transaction(
        self, connection: Any, project_id: UUID, order_id: UUID, data: TeamStart
    ) -> tuple[dict[str, Any], bool]:
        settings = self.gateway.live_text_settings
        if settings is None or self.gateway.actor_id != settings.operator_actor_id:
            raise Conflict("only the operator may start team work")
        lock_project(connection, project_id)
        try:
            saved = self._lookup(connection, project_id, order_id, data.client_message_id)
        except NotFound:
            saved = None
        if saved is not None:
            if saved.pop("body") != data.body:
                raise Conflict("start identity already belongs to a different message")
            return saved, False
        order = (
            connection.execute(
                text("SELECT status,version FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        if order["version"] != data.expected_version:
            raise Conflict("work order changed; refresh before starting")
        admission = TaskSessionAdmission(self.gateway)
        readiness = admission._inspect(connection, project_id, order_id, admit_draft=True)
        if readiness.blockers:
            raise Conflict("; ".join(readiness.blockers))
        if readiness.execution_mode != "legacy":
            raise Conflict("team work is already enabled; send or continue in its saved session")
        version = data.expected_version
        if order["status"] == "draft":
            version = mark_ready(self.gateway, connection, project_id, order_id)["version"]
        view = admission.activate_in_transaction(connection, project_id, order_id, version)
        session = view["session"]
        store = TaskSessionStore(self.gateway)
        message = SessionMessageCreate(
            client_message_id=data.client_message_id,
            expected_sequence=store._sequence(connection, project_id, session["id"]),
            body=data.body,
        )
        receipt, created = store.send_in_transaction(connection, project_id, order_id, message)
        if not created or receipt["state"] != "queued" or receipt["attempt_id"] is None:
            raise Conflict("start requires a new queued attempt; review the existing message owner")
        self.gateway._event(
            connection,
            project_id,
            order_id,
            None,
            "task_session_work_started",
            {key: str(value) if isinstance(value, UUID) else value for key, value in receipt.items()},
        )
        return receipt, created
