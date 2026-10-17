"""Project-bound work-order sessions and read-only legacy conversation import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.task_session_schemas import SessionContinueCreate, SessionMessageCreate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


@dataclass(frozen=True)
class TeamLimits:
    """Concurrent capacity, plan/depth bounds and automatic no-progress windows."""

    delegation_depth: int = 2
    assignments_per_order: int = 8
    plan_revisions: int = 3
    chief_decisions_per_order: int = 6
    retry_delay_seconds: int = 30

    @classmethod
    def of(cls, gateway: ToolGateway) -> TeamLimits:
        settings = gateway.live_text_settings
        if settings is None:
            return cls()
        return cls(
            delegation_depth=settings.team_max_delegation_depth,
            assignments_per_order=settings.team_max_assignments_per_order,
            plan_revisions=settings.team_max_plan_revisions,
            chief_decisions_per_order=settings.team_max_chief_decisions_per_order,
            retry_delay_seconds=settings.team_retry_delay_seconds,
        )


class TeamLimitReached(Conflict):
    """A configured team bound stopped further automatic work."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code.replace("_", " "))


class TaskSessionStore:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def _order(self, connection: Any, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT id,profile_id,parent_work_order_id,status FROM work_orders "
                    "WHERE project_id=:p AND id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("work order not found")
        return dict(row)

    def _existing(self, connection: Any, project_id: UUID, order_id: UUID) -> dict[str, Any] | None:
        row = (
            connection.execute(
                text(
                    "SELECT * FROM task_sessions WHERE project_id=:p AND work_order_id=:o AND is_order_owner"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def _import_legacy(
        self, connection: Any, project_id: UUID, order_id: UUID, session_id: UUID, root_id: UUID
    ) -> None:
        rows = connection.execute(
            text(
                "SELECT t.id,t.actor_id,t.operator_text,t.assistant_text,t.assistant_actor_id,"
                "t.status,t.profile_id,t.provider,t.model_id,t.provider_response_id,t.input_tokens,"
                "t.output_tokens,t.cost_usd,t.reason,t.created_at,t.finished_at "
                "FROM task_conversation_turns t JOIN task_conversations c "
                "ON c.project_id=t.project_id AND c.id=t.conversation_id "
                "WHERE c.project_id=:p AND c.work_order_id=:o "
                "ORDER BY t.created_at,t.id"
            ),
            {"p": project_id, "o": order_id},
        ).mappings()
        sequence = 0
        for turn in rows:
            sequence += 1
            operator_message_id = uuid4()
            connection.execute(
                text(
                    "INSERT INTO task_session_messages "
                    "(id,project_id,actor_id,root_work_order_id,session_id,source_turn_id,"
                    "sequence,kind,body,created_at) VALUES "
                    "(:id,:p,:actor,:root,:session,:turn,:sequence,'legacy_operator',:body,:created)"
                ),
                {
                    "id": operator_message_id,
                    "p": project_id,
                    "actor": turn["actor_id"],
                    "root": root_id,
                    "session": session_id,
                    "turn": turn["id"],
                    "sequence": sequence,
                    "body": turn["operator_text"],
                    "created": turn["created_at"],
                },
            )
            if turn["assistant_text"]:
                sequence += 1
                connection.execute(
                    text(
                        "INSERT INTO task_session_messages "
                        "(id,project_id,actor_id,root_work_order_id,session_id,source_turn_id,"
                        "sequence,kind,body,created_at) VALUES "
                        "(:id,:p,:actor,:root,:session,:turn,:sequence,'legacy_agent',:body,:created)"
                    ),
                    {
                        "id": uuid4(),
                        "p": project_id,
                        "actor": turn["assistant_actor_id"] or f"profile:{turn['profile_id']}",
                        "root": root_id,
                        "session": session_id,
                        "turn": turn["id"],
                        "sequence": sequence,
                        "body": turn["assistant_text"],
                        "created": turn["finished_at"] or turn["created_at"],
                    },
                )
            status = turn["status"] if turn["status"] != "running" else "unknown"
            connection.execute(
                text(
                    "INSERT INTO task_session_attempts "
                    "(id,project_id,actor_id,root_work_order_id,session_id,source_message_id,"
                    "status,provider,model_id,provider_turn_id,input_tokens,output_tokens,cost_usd,"
                    "reason,created_at,started_at,finished_at) VALUES "
                    "(:id,:p,:actor,:root,:session,:source,:status,:provider,:model,:turn,"
                    ":input,:output,:cost,:reason,:created,:created,:finished)"
                ),
                {
                    "id": uuid4(),
                    "p": project_id,
                    "actor": turn["actor_id"],
                    "root": root_id,
                    "session": session_id,
                    "source": operator_message_id,
                    "status": status,
                    "provider": turn["provider"],
                    "model": turn["model_id"],
                    "turn": turn["provider_response_id"],
                    "input": turn["input_tokens"],
                    "output": turn["output_tokens"],
                    "cost": turn["cost_usd"],
                    "reason": turn["reason"] if status != "unknown" else "legacy_outcome_unknown",
                    "created": turn["created_at"],
                    "finished": turn["finished_at"] or turn["created_at"],
                },
            )

    def _ensure(
        self, connection: Any, project_id: UUID, order_id: UUID, ancestors: frozenset[UUID] = frozenset()
    ) -> dict[str, Any]:
        if order_id in ancestors or len(ancestors) >= 30:
            raise Conflict("work order parent chain is invalid")
        existing = self._existing(connection, project_id, order_id)
        if existing is not None:
            return existing
        order = self._order(connection, project_id, order_id)
        parent_id = order["parent_work_order_id"]
        parent = (
            self._ensure(connection, project_id, parent_id, ancestors | {order_id})
            if parent_id is not None
            else None
        )
        session_id = uuid4()
        root_id = parent["root_work_order_id"] if parent is not None else order_id
        row = (
            connection.execute(
                text(
                    "INSERT INTO task_sessions "
                    "(id,project_id,actor_id,work_order_id,root_work_order_id,profile_id,"
                    "parent_session_id,kind) VALUES "
                    "(:id,:p,:actor,:order,:root,:profile,:parent,:kind) RETURNING *"
                ),
                {
                    "id": session_id,
                    "p": project_id,
                    "actor": self.gateway.actor_id,
                    "order": order_id,
                    "root": root_id,
                    "profile": order["profile_id"],
                    "parent": parent["id"] if parent is not None else None,
                    "kind": "child" if parent is not None else "main",
                },
            )
            .mappings()
            .one()
        )
        self._import_legacy(connection, project_id, order_id, session_id, root_id)
        self.gateway._event(
            connection,
            project_id,
            order_id,
            None,
            "task_session_opened",
            {"session_id": str(session_id), "kind": row["kind"]},
        )
        return dict(row)

    def open(self, project_id: UUID, order_id: UUID) -> tuple[dict[str, Any], bool]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            existing = self._existing(connection, project_id, order_id)
            session = existing or self._ensure(connection, project_id, order_id)
            return self._read(connection, project_id, session), existing is None

    def get(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            session = self._existing(connection, project_id, order_id)
            if session is None:
                self._order(connection, project_id, order_id)
                raise NotFound("team session has not been opened")
            return self._read(connection, project_id, session)

    def get_by_id(self, project_id: UUID, root_order_id: UUID, session_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            return self._read(
                connection,
                project_id,
                self._by_id(connection, project_id, root_order_id, session_id),
            )

    @staticmethod
    def _by_id(connection: Any, project_id: UUID, root_order_id: UUID, session_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT s.* FROM task_sessions s WHERE s.project_id=:p AND s.id=:session "
                    "AND (s.root_work_order_id=:root OR (s.work_order_id=:root AND s.is_order_owner))"
                ),
                {"p": project_id, "root": root_order_id, "session": session_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("agent session not found")
        return dict(row)

    @staticmethod
    def _waiting_for_dependency(connection: Any, project_id: UUID, session_id: UUID) -> bool:
        state = connection.scalar(
            text("SELECT status FROM task_session_assignments WHERE project_id=:p AND child_session_id=:s"),
            {"p": project_id, "s": session_id},
        )
        return bool(state == "pending")

    @staticmethod
    def _sequence(connection: Any, project_id: UUID, session_id: UUID) -> int:
        return int(
            connection.scalar(
                text(
                    "SELECT coalesce(max(sequence),0) FROM task_session_messages "
                    "WHERE project_id=:p AND session_id=:s"
                ),
                {"p": project_id, "s": session_id},
            )
        )

    @staticmethod
    def _active_attempt(connection: Any, project_id: UUID, session_id: UUID) -> dict[str, Any] | None:
        row = (
            connection.execute(
                text(
                    "SELECT id,status FROM task_session_attempts WHERE project_id=:p AND session_id=:s "
                    "AND status IN ('queued','running')"
                ),
                {"p": project_id, "s": session_id},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    def _require_team_mode(session: dict[str, Any]) -> None:
        if session["execution_mode"] != "team":
            raise Conflict("team execution is not enabled for this work order")

    def _queue_attempt(
        self,
        connection: Any,
        session: dict[str, Any],
        *,
        source_message_id: UUID | None = None,
        client_attempt_id: UUID | None = None,
        retry_of_attempt_id: UUID | None = None,
        delay_seconds: int = 0,
        purpose: Literal["work", "discussion"] = "work",
    ) -> UUID:
        from onefoundry.task_session_uncertainty import unresolved

        if purpose == "discussion":
            from onefoundry.agent_conversation_policy import allows_main_discussion

            owner = self._existing(connection, session["project_id"], session["root_work_order_id"])
            if (
                owner is None
                or owner["id"] != session["id"]
                or not allows_main_discussion(
                    connection, session["project_id"], session["root_work_order_id"], owner["profile_id"]
                )
            ):
                raise Conflict(
                    "discussion turns require the canonical root Chief or an "
                    "operator-created direct conversation"
                )
        elif unresolved(connection, session["project_id"], session_id=session["id"]):
            raise Conflict("acknowledge the unknown outcome before starting a separate turn")
        attempt_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO task_session_attempts "
                "(id,project_id,actor_id,root_work_order_id,session_id,source_message_id,client_attempt_id,"
                "retry_of_attempt_id,not_before,purpose) VALUES "
                "(:id,:p,:actor,:root,:session,:message,:request,"
                ":retry,CASE WHEN :delay>0 THEN clock_timestamp()+(:delay * interval "
                "'1 second') END,:purpose)"
            ),
            {
                "id": attempt_id,
                "p": session["project_id"],
                "actor": self.gateway.actor_id,
                "root": session["root_work_order_id"],
                "session": session["id"],
                "message": source_message_id,
                "request": client_attempt_id,
                "retry": retry_of_attempt_id,
                "delay": delay_seconds,
                "purpose": purpose,
            },
        )
        connection.execute(
            text("UPDATE task_sessions SET status='working' WHERE project_id=:p AND id=:s"),
            {"p": session["project_id"], "s": session["id"]},
        )
        self.gateway._event(
            connection,
            session["project_id"],
            session["work_order_id"],
            None,
            "task_session_attempt_queued",
            {"session_id": str(session["id"]), "attempt_id": str(attempt_id)},
        )
        return attempt_id

    def send(
        self,
        project_id: UUID,
        order_id: UUID,
        data: SessionMessageCreate,
        *,
        session_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.gateway.engine.begin() as connection:
            return self.send_in_transaction(connection, project_id, order_id, data, session_id=session_id)

    def send_in_transaction(
        self,
        connection: Any,
        project_id: UUID,
        order_id: UUID,
        data: SessionMessageCreate,
        *,
        session_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Save and queue through the existing message owner in a caller's transaction."""
        lock_project(connection, project_id)
        session = (
            self._ensure(connection, project_id, order_id)
            if session_id is None
            else self._by_id(connection, project_id, order_id, session_id)
        )
        connection.execute(
            text("SELECT id FROM task_sessions WHERE project_id=:p AND id=:s FOR UPDATE"),
            {"p": project_id, "s": session["id"]},
        )
        prior = (
            connection.execute(
                text(
                    "SELECT id,session_id,body,sequence FROM task_session_messages "
                    "WHERE project_id=:p AND client_message_id=:client"
                ),
                {"p": project_id, "client": data.client_message_id},
            )
            .mappings()
            .one_or_none()
        )
        if prior is not None:
            if prior["session_id"] != session["id"] or prior["body"] != data.body:
                raise Conflict("message identity already belongs to a different request")
            attempt = (
                connection.execute(
                    text(
                        "SELECT id FROM task_session_attempts WHERE project_id=:p "
                        "AND source_message_id=:message"
                    ),
                    {"p": project_id, "message": prior["id"]},
                )
                .mappings()
                .one_or_none()
            )
            return {
                "session_id": session["id"],
                "message_id": prior["id"],
                "attempt_id": attempt["id"] if attempt is not None else None,
                "sequence": prior["sequence"],
                "state": (
                    "queued"
                    if attempt is not None
                    else "waiting_for_input"
                    if self._waiting_for_dependency(connection, project_id, session["id"])
                    else "coalesced"
                ),
            }, False
        self._require_team_mode(session)
        order = self._order(connection, project_id, session["work_order_id"])
        if session["status"] == "closed" or order["status"] in (
            "done",
            "draft",
            "blocked",
            "needs_review",
            "failed",
            "unknown",
        ):
            raise Conflict("work order is closed; reopen it before continuing")
        return self._save_message(connection, project_id, session, data)

    def _save_message(
        self,
        connection: Any,
        project_id: UUID,
        session: dict[str, Any],
        data: SessionMessageCreate,
        *,
        purpose: Literal["work", "discussion"] = "work",
        defer: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Persist after the owning caller validates lifecycle, scope and idempotency."""
        sequence = self._sequence(connection, project_id, session["id"])
        if sequence != data.expected_sequence:
            raise Conflict("session sequence changed; refresh before sending")
        settings = self.gateway.live_text_settings
        context_limit = settings.onefoundry_max_context_bytes if settings is not None else None
        if context_limit is not None and len(data.body.encode("utf-8")) > context_limit:
            raise Conflict("operator message exceeds configured context budget")
        message_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO task_session_messages "
                "(id,project_id,actor_id,root_work_order_id,session_id,client_message_id,"
                "sequence,kind,body) VALUES "
                "(:id,:p,:actor,:root,:session,:client,:sequence,'operator',:body)"
            ),
            {
                "id": message_id,
                "p": project_id,
                "actor": self.gateway.actor_id,
                "root": session["root_work_order_id"],
                "session": session["id"],
                "client": data.client_message_id,
                "sequence": sequence + 1,
                "body": data.body,
            },
        )
        active = self._active_attempt(connection, project_id, session["id"])
        waiting_for_dependency = self._waiting_for_dependency(connection, project_id, session["id"])
        attempt_id = (
            None
            if active is not None or waiting_for_dependency or defer
            else self._queue_attempt(connection, session, source_message_id=message_id, purpose=purpose)
        )
        self.gateway._event(
            connection,
            project_id,
            session["work_order_id"],
            None,
            "task_session_message_saved",
            {"session_id": str(session["id"]), "message_id": str(message_id)},
        )
        return {
            "session_id": session["id"],
            "message_id": message_id,
            "attempt_id": attempt_id,
            "sequence": sequence + 1,
            "state": (
                "queued"
                if attempt_id is not None
                else "waiting_for_input"
                if waiting_for_dependency or defer
                else "coalesced"
            ),
        }, True

    def continue_work(
        self,
        project_id: UUID,
        order_id: UUID,
        data: SessionContinueCreate,
        *,
        session_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            session = (
                self._ensure(connection, project_id, order_id)
                if session_id is None
                else self._by_id(connection, project_id, order_id, session_id)
            )
            connection.execute(
                text("SELECT id FROM task_sessions WHERE project_id=:p AND id=:s FOR UPDATE"),
                {"p": project_id, "s": session["id"]},
            )
            prior = (
                connection.execute(
                    text(
                        "SELECT id,session_id FROM task_session_attempts "
                        "WHERE project_id=:p AND client_attempt_id=:client"
                    ),
                    {"p": project_id, "client": data.client_attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None:
                if prior["session_id"] != session["id"]:
                    raise Conflict("continuation identity belongs to another session")
                return {
                    "session_id": session["id"],
                    "message_id": None,
                    "attempt_id": prior["id"],
                    "sequence": self._sequence(connection, project_id, session["id"]),
                    "state": "queued",
                }, False
            self._require_team_mode(session)
            order = self._order(connection, project_id, session["work_order_id"])
            if session["status"] == "closed" or order["status"] in (
                "done",
                "draft",
                "blocked",
                "needs_review",
                "failed",
                "unknown",
            ):
                raise Conflict("work order is closed; reopen it before continuing")
            if self._active_attempt(connection, project_id, session["id"]) is not None:
                raise Conflict("agent is already queued or working")
            if self._waiting_for_dependency(connection, project_id, session["id"]):
                raise Conflict("agent is waiting for its declared input")
            attempt_id = self._queue_attempt(connection, session, client_attempt_id=data.client_attempt_id)
            return {
                "session_id": session["id"],
                "message_id": None,
                "attempt_id": attempt_id,
                "sequence": self._sequence(connection, project_id, session["id"]),
                "state": "queued",
            }, True

    def _read(self, connection: Any, project_id: UUID, session: dict[str, Any]) -> dict[str, Any]:
        from onefoundry.task_session_media_wait import recording_wait_reason

        session = {
            **session,
            "waiting_reason": recording_wait_reason(connection, project_id, session["id"])
            if session["status"] == "waiting_for_operator"
            else None,
        }
        messages = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,project_id,session_id,source_session_id,source_turn_id,"
                    "client_message_id,sequence,actor_id,kind,body,created_at "
                    "FROM task_session_messages WHERE project_id=:p AND session_id=:s "
                    "AND sequence IN (SELECT sequence FROM task_session_messages "
                    "WHERE project_id=:p AND session_id=:s ORDER BY sequence DESC LIMIT 500) "
                    "ORDER BY sequence"
                ),
                {"p": project_id, "s": session["id"]},
            ).mappings()
        ]
        attempts = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,project_id,session_id,source_message_id,client_attempt_id,"
                    "input_sequence,status,purpose,provider,model_id,"
                    "provider_thread_id,provider_turn_id,prompt_sha256,context_message_ids,"
                    "context_dependency_ids,context_omitted_count,input_tokens,output_tokens,cost_usd,"
                    "reason,retry_of_attempt_id,not_before,created_at,started_at,finished_at,"
                    "EXISTS(SELECT 1 FROM events e WHERE e.project_id=task_session_attempts.project_id "
                    "AND e.work_order_id=task_session_attempts.root_work_order_id "
                    "AND e.event_type='task_session_unknown_acknowledged' "
                    "AND e.payload->>'attempt_id'=task_session_attempts.id::text) AS unknown_acknowledged "
                    "FROM task_session_attempts "
                    "WHERE project_id=:p AND session_id=:s AND (status IN ('queued','running') "
                    "OR (status='unknown' AND NOT EXISTS (SELECT 1 FROM events e "
                    "WHERE e.project_id=task_session_attempts.project_id "
                    "AND e.work_order_id=task_session_attempts.root_work_order_id "
                    "AND e.event_type='task_session_unknown_acknowledged' "
                    "AND e.payload->>'attempt_id'=task_session_attempts.id::text)) "
                    "OR id IN (SELECT id FROM task_session_attempts WHERE project_id=:p "
                    "AND session_id=:s ORDER BY created_at DESC,id DESC LIMIT 500)) "
                    "ORDER BY created_at,id"
                ),
                {"p": project_id, "s": session["id"]},
            ).mappings()
        ]
        children = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,project_id,work_order_id,root_work_order_id,profile_id,"
                    "parent_session_id,kind,status,execution_mode,is_order_owner,created_at,"
                    "(SELECT w.status FROM work_orders w WHERE w.project_id=task_sessions.project_id "
                    "AND w.id=task_sessions.work_order_id) AS work_order_status FROM task_sessions "
                    "WHERE project_id=:p AND root_work_order_id=:root AND id<>:s "
                    "ORDER BY created_at,id"
                ),
                {"p": project_id, "root": session["root_work_order_id"], "s": session["id"]},
            ).mappings()
        ]
        assignments = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,project_id,root_work_order_id,parent_session_id,child_session_id,"
                    "plan_revision,objective,expected_result,acceptance_check,acceptance_kind,status,"
                    "result_message_id,created_at FROM task_session_assignments "
                    "WHERE project_id=:p AND root_work_order_id=:root ORDER BY created_at,id"
                ),
                {"p": project_id, "root": session["root_work_order_id"]},
            ).mappings()
        ]
        dependencies = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT assignment_id,depends_on_id FROM task_session_dependencies "
                    "WHERE project_id=:p AND root_work_order_id=:root ORDER BY assignment_id,depends_on_id"
                ),
                {"p": project_id, "root": session["root_work_order_id"]},
            ).mappings()
        ]
        decision_causes = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,main_session_id,plan_revision,kind,detail,dispatch_attempt_id,"
                    "resolved_at,handled_at,created_at "
                    "FROM task_session_decision_causes WHERE project_id=:p AND root_work_order_id=:root "
                    "AND id IN (SELECT id FROM task_session_decision_causes "
                    "WHERE project_id=:p AND root_work_order_id=:root "
                    "ORDER BY created_at DESC,id DESC LIMIT 500) ORDER BY created_at,id"
                ),
                {"p": project_id, "root": session["root_work_order_id"]},
            ).mappings()
        ]
        tool_calls = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,session_id,attempt_id,tool_name,status,result,reason,created_at,finished_at "
                    "FROM task_session_tool_calls WHERE project_id=:p AND session_id=:session "
                    "AND (status='started' OR id IN (SELECT id FROM task_session_tool_calls "
                    "WHERE project_id=:p AND session_id=:session "
                    "ORDER BY created_at DESC,id DESC LIMIT 500)) "
                    "ORDER BY created_at,id"
                ),
                {"p": project_id, "session": session["id"]},
            ).mappings()
        ]
        totals = (
            connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM task_session_messages "
                    "WHERE project_id=:p AND session_id=:s) AS messages,"
                    "(SELECT count(*) FROM task_session_attempts "
                    "WHERE project_id=:p AND session_id=:s) AS attempts,"
                    "(SELECT count(*) FROM task_session_decision_causes "
                    "WHERE project_id=:p AND root_work_order_id=:root) AS decision_causes,"
                    "(SELECT count(*) FROM task_session_tool_calls "
                    "WHERE project_id=:p AND session_id=:s) AS tool_calls"
                ),
                {"p": project_id, "s": session["id"], "root": session["root_work_order_id"]},
            )
            .mappings()
            .one()
        )
        return {
            "session": {
                **session,
                "work_order_status": self._order(connection, project_id, session["work_order_id"])["status"],
            },
            "messages": messages,
            "attempts": attempts,
            "children": children,
            "assignments": assignments,
            "dependencies": dependencies,
            "decision_causes": decision_causes,
            "tool_calls": tool_calls,
            "history_omitted": {
                "messages": max(0, int(totals["messages"]) - len(messages)),
                "attempts": max(0, int(totals["attempts"]) - len(attempts)),
                "decision_causes": max(0, int(totals["decision_causes"]) - len(decision_causes)),
                "tool_calls": max(0, int(totals["tool_calls"]) - len(tool_calls)),
            },
        }
