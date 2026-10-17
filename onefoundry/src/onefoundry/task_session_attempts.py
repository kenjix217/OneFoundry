"""Claim and settle bounded team turns without closing the work-order objective."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_store import TaskSessionStore, TeamLimits
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

Outcome = Literal["done", "failed", "unknown"]


class TaskSessionAttempts:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        self.sessions = TaskSessionStore(gateway)

    def claim_next(self, project_id: UUID | None = None) -> dict[str, Any] | None:
        """Claim one queued turn; this never launches a model in the app process."""
        with self.gateway.engine.begin() as connection:
            candidate = (
                connection.execute(
                    text(
                        "SELECT a.id,a.project_id FROM task_session_attempts a "
                        "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                        "JOIN work_orders w ON w.project_id=a.project_id "
                        "AND w.id=a.root_work_order_id "
                        "WHERE a.status='queued' AND s.execution_mode='team' "
                        "AND (a.not_before IS NULL OR a.not_before<=clock_timestamp()) "
                        "AND (w.status IN ('ready','running') OR a.purpose='discussion') "
                        "AND NOT EXISTS(SELECT 1 FROM chief_conversation_controls cc WHERE "
                        "cc.project_id=a.project_id AND cc.work_order_id=a.root_work_order_id AND "
                        "cc.paused) "
                        "AND NOT EXISTS(SELECT 1 FROM coordination_controls c "
                        "WHERE c.project_id=a.project_id AND c.paused) "
                        "AND (CAST(:p AS uuid) IS NULL OR a.project_id=:p) "
                        "ORDER BY a.created_at,a.id LIMIT 1"
                    ),
                    {"p": project_id},
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None:
                return None
            lock_project(connection, candidate["project_id"])
            row = (
                connection.execute(
                    text(
                        "SELECT "
                        "a.id,a.project_id,a.session_id,a.root_work_order_id,a.purpose,s.work_order_id,"
                        "s.profile_id,s.provider_thread_id,s.kind,w.status AS work_order_status "
                        "FROM task_session_attempts a "
                        "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                        "JOIN work_orders w ON w.project_id=a.project_id "
                        "AND w.id=a.root_work_order_id "
                        "WHERE a.project_id=:p AND a.id=:a AND a.status='queued' "
                        "AND (a.not_before IS NULL OR a.not_before<=clock_timestamp()) "
                        "AND s.execution_mode='team' AND (w.status IN ('ready','running') OR "
                        "a.purpose='discussion') "
                        "AND NOT EXISTS(SELECT 1 FROM chief_conversation_controls cc WHERE "
                        "cc.project_id=a.project_id AND cc.work_order_id=a.root_work_order_id AND "
                        "cc.paused) "
                        "AND NOT EXISTS(SELECT 1 FROM coordination_controls c "
                        "WHERE c.project_id=a.project_id AND c.paused) "
                        "FOR UPDATE OF a,s,w SKIP LOCKED"
                    ),
                    {"p": candidate["project_id"], "a": candidate["id"]},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            if row["work_order_status"] == "ready" and row["purpose"] == "work":
                connection.execute(
                    text(
                        "UPDATE work_orders SET status='running' WHERE project_id=:p AND id=:root "
                        "AND status='ready'"
                    ),
                    {"p": row["project_id"], "root": row["root_work_order_id"]},
                )
                self.gateway._event(
                    connection,
                    row["project_id"],
                    row["root_work_order_id"],
                    None,
                    "task_session_work_started",
                    {"session_id": str(row["session_id"]), "attempt_id": str(row["id"])},
                )
            input_sequence = self.sessions._sequence(connection, row["project_id"], row["session_id"])
            connection.execute(
                text(
                    "UPDATE task_session_attempts SET status='running',input_sequence=:sequence,"
                    "started_at=clock_timestamp() WHERE project_id=:p AND id=:a"
                ),
                {"p": row["project_id"], "a": row["id"], "sequence": input_sequence},
            )
            if row["kind"] == "main" and row["purpose"] == "work":
                TaskSessionDecisions.attach_to_attempt(
                    connection, row["project_id"], row["session_id"], row["id"]
                )
            connection.execute(
                text(
                    "UPDATE task_session_assignments SET status='active' "
                    "WHERE project_id=:p AND child_session_id=:s AND status IN ('ready','blocked')"
                ),
                {"p": row["project_id"], "s": row["session_id"]},
            )
            self.gateway._event(
                connection,
                row["project_id"],
                row["work_order_id"],
                None,
                "task_session_attempt_started",
                {"session_id": str(row["session_id"]), "attempt_id": str(row["id"])},
            )
            return {**dict(row), "input_sequence": input_sequence}

    def expire_stale(
        self,
        max_elapsed_seconds: int,
        project_id: UUID | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        """Make abandoned bounded turns continuable without replaying the provider request."""
        if type(max_elapsed_seconds) is not int or not 30 <= max_elapsed_seconds <= 3600:
            raise ValueError("invalid team turn elapsed bound")
        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("team turn clock must include a timezone")
        cutoff = observed_at - timedelta(seconds=max_elapsed_seconds)
        with self.gateway.engine.connect() as connection:
            candidates = (
                connection.execute(
                    text(
                        "SELECT a.project_id,a.id FROM task_session_attempts a "
                        "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                        "WHERE a.status='running' AND s.execution_mode='team' "
                        "AND a.started_at<=:cutoff "
                        "AND (CAST(:p AS uuid) IS NULL OR a.project_id=:p) "
                        "ORDER BY a.started_at,a.id LIMIT 30"
                    ),
                    {"p": project_id, "cutoff": cutoff},
                )
                .mappings()
                .all()
            )
        settled = 0
        for candidate in candidates:
            try:
                self.settle(candidate["project_id"], candidate["id"], "unknown", reason="runner_interrupted")
            except Conflict:
                # Another worker settled the attempt after the read.
                continue
            settled += 1
        return settled

    def bind_provider_thread(self, project_id: UUID, attempt_id: UUID, thread_id: str) -> None:
        """Save a new provider thread before starting its first turn."""
        self._bind_provider_identity(project_id, attempt_id, "thread", thread_id)

    def bind_provider_turn(self, project_id: UUID, attempt_id: UUID, turn_id: str) -> None:
        """Save a provider turn as soon as the launch response is received."""
        self._bind_provider_identity(project_id, attempt_id, "turn", turn_id)

    def save_public_update(
        self, project_id: UUID, attempt_id: UUID, provider_item_id: str, body: str
    ) -> UUID:
        """Save a completed public agent message once, scoped to its active turn."""
        if not provider_item_id or len(provider_item_id) > 128:
            raise ValueError("invalid public update identity")
        body = local_text(body)
        if len(body) > 4000 or contains_sensitive_text(body):
            raise ValueError("invalid public update")
        client_id = uuid5(NAMESPACE_URL, f"onefoundry:public-update:{attempt_id}:{provider_item_id}")
        message_id = uuid5(NAMESPACE_URL, f"onefoundry:message:{client_id}")
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            prior = (
                connection.execute(
                    text(
                        "SELECT id,session_id,body FROM task_session_messages "
                        "WHERE project_id=:p AND client_message_id=:client"
                    ),
                    {"p": project_id, "client": client_id},
                )
                .mappings()
                .one_or_none()
            )
            row = (
                connection.execute(
                    text(
                        "SELECT a.session_id,a.root_work_order_id,s.work_order_id,s.profile_id "
                        "FROM task_session_attempts a JOIN task_sessions s "
                        "ON s.project_id=a.project_id AND s.id=a.session_id "
                        "WHERE a.project_id=:p AND a.id=:a AND a.status='running' "
                        "AND s.execution_mode='team'"
                    ),
                    {"p": project_id, "a": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise Conflict("public update is outside an active team turn")
            if prior is not None:
                if (
                    prior["id"] != message_id
                    or prior["session_id"] != row["session_id"]
                    or prior["body"] != body
                ):
                    raise Conflict("public update identity changed")
                return message_id
            sequence = self.sessions._sequence(connection, project_id, row["session_id"]) + 1
            connection.execute(
                text(
                    "INSERT INTO task_session_messages "
                    "(id,project_id,actor_id,root_work_order_id,session_id,client_message_id,"
                    "sequence,kind,body) VALUES "
                    "(:id,:p,:actor,:root,:session,:client,:sequence,'agent',:body)"
                ),
                {
                    "id": message_id,
                    "p": project_id,
                    "actor": f"profile:{row['profile_id']}",
                    "root": row["root_work_order_id"],
                    "session": row["session_id"],
                    "client": client_id,
                    "sequence": sequence,
                    "body": body,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                None,
                "task_session_update_published",
                {"session_id": str(row["session_id"]), "message_id": str(message_id)},
            )
            return message_id

    def replace_missing_thread(self, project_id: UUID, attempt_id: UUID, missing_thread: str) -> None:
        """Replace a missing rollout after prior local action outcomes are settled."""
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            row = (
                connection.execute(
                    text(
                        "SELECT a.status,a.provider_thread_id,a.provider_turn_id,"
                        "s.id AS session_id,s.provider_thread_id AS saved_thread,s.work_order_id "
                        "FROM task_session_attempts a JOIN task_sessions s "
                        "ON s.project_id=a.project_id AND s.id=a.session_id "
                        "WHERE a.project_id=:p AND a.id=:a AND s.execution_mode='team' "
                        "FOR UPDATE OF a,s"
                    ),
                    {"p": project_id, "a": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("team attempt not found")
            if (
                row["status"] != "running"
                or row["provider_thread_id"] is not None
                or row["provider_turn_id"] is not None
                or row["saved_thread"] != missing_thread
            ):
                raise Conflict("saved thread cannot be replaced during an active provider turn")
            previous = connection.scalar(
                text(
                    "SELECT id FROM task_session_attempts WHERE project_id=:p AND session_id=:s "
                    "AND id<>:a AND status IN ('done','failed','unknown') "
                    "AND provider_thread_id=:thread LIMIT 1"
                ),
                {"p": project_id, "s": row["session_id"], "a": attempt_id, "thread": missing_thread},
            )
            if previous is None:
                raise Conflict("saved provider thread has no settled history")
            unresolved = connection.scalar(
                text(
                    "SELECT id FROM task_session_tool_calls WHERE project_id=:p AND session_id=:s "
                    "AND status='started' LIMIT 1"
                ),
                {"p": project_id, "s": row["session_id"]},
            )
            if unresolved is not None:
                raise Conflict("saved team action needs reconciliation before provider history replacement")
            connection.execute(
                text("UPDATE task_sessions SET provider_thread_id=NULL WHERE project_id=:p AND id=:s"),
                {"p": project_id, "s": row["session_id"]},
            )
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                None,
                "task_session_missing_thread_replaced",
                {"attempt_id": str(attempt_id), "session_id": str(row["session_id"])},
            )

    def _bind_provider_identity(
        self, project_id: UUID, attempt_id: UUID, kind: Literal["thread", "turn"], value: str
    ) -> None:
        if not value or len(value) > 200 or any(ord(char) < 33 for char in value):
            raise ValueError("invalid provider identity")
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            row = (
                connection.execute(
                    text(
                        "SELECT a.status,a.provider_thread_id,a.provider_turn_id,s.id AS session_id,"
                        "s.provider_thread_id AS saved_thread,s.work_order_id,s.execution_mode "
                        "FROM task_session_attempts a JOIN task_sessions s "
                        "ON s.project_id=a.project_id AND s.id=a.session_id "
                        "WHERE a.project_id=:p AND a.id=:a FOR UPDATE OF a,s"
                    ),
                    {"p": project_id, "a": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("team attempt not found")
            if row["status"] != "running" or row["execution_mode"] != "team":
                raise Conflict("team attempt is not running")
            if kind == "thread":
                if row["saved_thread"] not in (None, value) or row["provider_thread_id"] not in (
                    None,
                    value,
                ):
                    raise Conflict("provider thread changed for the session")
                if row["provider_thread_id"] == value:
                    return
                connection.execute(
                    text("UPDATE task_sessions SET provider_thread_id=:id WHERE project_id=:p AND id=:s"),
                    {"p": project_id, "s": row["session_id"], "id": value},
                )
                connection.execute(
                    text(
                        "UPDATE task_session_attempts SET provider_thread_id=:id "
                        "WHERE project_id=:p AND id=:a"
                    ),
                    {"p": project_id, "a": attempt_id, "id": value},
                )
            else:
                if row["saved_thread"] is None or row["provider_thread_id"] != row["saved_thread"]:
                    raise Conflict("provider thread must be saved before its turn")
                if row["provider_turn_id"] not in (None, value):
                    raise Conflict("provider turn changed for the attempt")
                if row["provider_turn_id"] == value:
                    return
                connection.execute(
                    text(
                        "UPDATE task_session_attempts SET provider_turn_id=:id WHERE project_id=:p AND id=:a"
                    ),
                    {"p": project_id, "a": attempt_id, "id": value},
                )
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                None,
                f"task_session_provider_{kind}_bound",
                {"attempt_id": str(attempt_id), "session_id": str(row["session_id"])},
            )

    def settle(
        self,
        project_id: UUID,
        attempt_id: UUID,
        outcome: Outcome,
        *,
        reason: str | None = None,
        public_reply: str | None = None,
        provider: str | None = None,
        model_id: str | None = None,
        provider_thread_id: str | None = None,
        provider_turn_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        """Settle one turn. `retryable` means the provider provably never processed it."""
        if outcome == "done":
            if reason is not None or public_reply is None:
                raise ValueError("a completed turn requires a public reply and no error reason")
            public_reply = local_text(public_reply)
            if len(public_reply) > 16000:
                raise ValueError("public reply exceeds the saved message limit")
        elif reason is None or public_reply is not None:
            raise ValueError("a stopped turn requires a reason and no reply")
        if reason is not None and (len(reason) > 160 or not reason.replace("_", "").isalnum()):
            raise ValueError("invalid turn reason")
        if any(value is not None and value < 0 for value in (input_tokens, output_tokens)):
            raise ValueError("invalid token usage")
        if retryable and outcome != "failed":
            raise ValueError("only a failed turn can be retried")
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            row = (
                connection.execute(
                    text(
                        "SELECT "
                        "a.*,s.kind,s.work_order_id,s.profile_id,s.execution_mode,s.status AS "
                        "session_status "
                        "FROM task_session_attempts a JOIN task_sessions s "
                        "ON s.project_id=a.project_id AND s.id=a.session_id "
                        "WHERE a.project_id=:p AND a.id=:a FOR UPDATE OF a,s"
                    ),
                    {"p": project_id, "a": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("team attempt not found")
            if row["execution_mode"] != "team" or row["status"] != "running":
                raise Conflict("team attempt is not running")
            if provider_thread_id is not None and row["provider_thread_id"] not in (None, provider_thread_id):
                raise Conflict("provider thread changed for the attempt")
            if provider_turn_id is not None and row["provider_turn_id"] not in (None, provider_turn_id):
                raise Conflict("provider turn changed for the attempt")
            provider_thread_id = provider_thread_id or row["provider_thread_id"]
            provider_turn_id = provider_turn_id or row["provider_turn_id"]
            pending = (
                connection.execute(
                    text(
                        "SELECT id,sequence FROM task_session_messages WHERE project_id=:p AND session_id=:s "
                        "AND kind='operator' AND sequence>:input ORDER BY sequence DESC LIMIT 1"
                    ),
                    {"p": project_id, "s": row["session_id"], "input": row["input_sequence"]},
                )
                .mappings()
                .one_or_none()
            )
            retry = (
                retryable
                and pending is None
                and row["retry_of_attempt_id"] is None
                and connection.scalar(
                    text(
                        "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND attempt_id=:a LIMIT 1"
                    ),
                    {"p": project_id, "a": attempt_id},
                )
                is None
            )
            connection.execute(
                text(
                    "UPDATE task_session_attempts SET status=:outcome,reason=:reason,provider=:provider,"
                    "model_id=:model,provider_thread_id=:thread,provider_turn_id=:turn,"
                    "input_tokens=:input,output_tokens=:output,finished_at=clock_timestamp() "
                    "WHERE project_id=:p AND id=:a"
                ),
                {
                    "p": project_id,
                    "a": attempt_id,
                    "outcome": outcome,
                    "reason": reason,
                    "provider": provider,
                    "model": model_id,
                    "thread": provider_thread_id,
                    "turn": provider_turn_id,
                    "input": input_tokens,
                    "output": output_tokens,
                },
            )
            from onefoundry.task_session_runs import settle as settle_run

            settle_run(connection, project_id, attempt_id, outcome, reason, input_tokens, output_tokens)
            from onefoundry.task_session_media_wait import waiting_for_recording

            media_wait = outcome == "done" and waiting_for_recording(
                connection, project_id, row["session_id"]
            )
            from onefoundry.self_extension_dispatch import waiting as waiting_for_extension

            extension_wait = outcome == "done" and waiting_for_extension(
                connection, project_id, row["session_id"]
            )
            if not retry and not media_wait and not extension_wait:
                # A pending retry keeps the assignment active and does not wake the Chief.
                connection.execute(
                    text(
                        "UPDATE task_session_assignments SET status='blocked' "
                        "WHERE project_id=:p AND child_session_id=:s AND status='active'"
                    ),
                    {"p": project_id, "s": row["session_id"]},
                )
                TaskSessionDecisions(self.gateway).record_blocker(
                    connection,
                    project_id,
                    row["session_id"],
                    attempt_id,
                    reason or "no_confirmed_result",
                )
            if outcome == "done" and row["prompt_sha256"] is not None:
                TaskSessionDecisions.mark_handled(connection, project_id, row["session_id"])
            if provider_thread_id is not None:
                saved_thread = connection.scalar(
                    text("SELECT provider_thread_id FROM task_sessions WHERE project_id=:p AND id=:s"),
                    {"p": project_id, "s": row["session_id"]},
                )
                if saved_thread is not None and saved_thread != provider_thread_id:
                    raise Conflict("provider thread changed for the session")
                connection.execute(
                    text("UPDATE task_sessions SET provider_thread_id=:thread WHERE project_id=:p AND id=:s"),
                    {"p": project_id, "s": row["session_id"], "thread": provider_thread_id},
                )
            reply_id: UUID | None = None
            if public_reply is not None:
                reply_id = uuid4()
                sequence = self.sessions._sequence(connection, project_id, row["session_id"]) + 1
                connection.execute(
                    text(
                        "INSERT INTO task_session_messages "
                        "(id,project_id,actor_id,root_work_order_id,session_id,sequence,kind,body) "
                        "VALUES (:id,:p,:actor,:root,:session,:sequence,'agent',:body)"
                    ),
                    {
                        "id": reply_id,
                        "p": project_id,
                        "actor": f"profile:{row['profile_id']}",
                        "root": row["root_work_order_id"],
                        "session": row["session_id"],
                        "sequence": sequence,
                        "body": public_reply,
                    },
                )
            next_attempt_id: UUID | None = None
            session = {
                "id": row["session_id"],
                "project_id": project_id,
                "root_work_order_id": row["root_work_order_id"],
                "work_order_id": row["work_order_id"],
            }
            if row["session_status"] == "closed":
                # A confirmed child result is final for that assignment. Retain any
                # coalesced operator message without silently launching a new turn.
                pass
            elif pending is not None:
                from onefoundry.task_session_followup import purpose

                followup = purpose(connection, project_id, row, pending["id"])
                if followup is not None:
                    next_attempt_id = self.sessions._queue_attempt(
                        connection, session, source_message_id=pending["id"], purpose=followup
                    )
                else:
                    connection.execute(
                        text(
                            "UPDATE task_sessions SET status='waiting_for_operator' WHERE "
                            "project_id=:p AND id=:s"
                        ),
                        {"p": project_id, "s": row["session_id"]},
                    )
            elif retry:
                next_attempt_id = self.sessions._queue_attempt(
                    connection,
                    session,
                    retry_of_attempt_id=attempt_id,
                    purpose=row["purpose"],
                    delay_seconds=TeamLimits.of(self.gateway).retry_delay_seconds,
                )
            else:
                connection.execute(
                    text("UPDATE task_sessions SET status=:status WHERE project_id=:p AND id=:s"),
                    {
                        "p": project_id,
                        "s": row["session_id"],
                        "status": "waiting_for_operator" if media_wait else "open",
                    },
                )
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                None,
                "task_session_attempt_finished",
                {
                    "session_id": str(row["session_id"]),
                    "attempt_id": str(attempt_id),
                    "outcome": outcome,
                    "reply_id": str(reply_id) if reply_id is not None else None,
                    "next_attempt_id": str(next_attempt_id) if next_attempt_id is not None else None,
                    "retry": retry,
                },
            )
            return {
                "attempt_id": attempt_id,
                "session_id": row["session_id"],
                "outcome": outcome,
                "reply_id": reply_id,
                "next_attempt_id": next_attempt_id,
            }
