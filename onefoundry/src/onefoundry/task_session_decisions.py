"""Deterministic Chief wakeups from saved child causes, with no model call here."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.task_session_progress import decisions_without_progress
from onefoundry.task_session_store import TaskSessionStore, TeamLimits

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class TaskSessionDecisions:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        self.sessions = TaskSessionStore(gateway)

    @staticmethod
    def _main(connection: Any, project_id: UUID, root_order_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    "SELECT id,work_order_id,root_work_order_id,execution_mode,status FROM task_sessions "
                    "WHERE project_id=:p AND root_work_order_id=:root AND kind='main' "
                    "AND is_order_owner FOR UPDATE"
                ),
                {"p": project_id, "root": root_order_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["execution_mode"] != "team":
            raise NotFound("team Chief session not found")
        return dict(row)

    def record_blocker(
        self,
        connection: Any,
        project_id: UUID,
        child_session_id: UUID,
        source_attempt_id: UUID,
        reason: str,
    ) -> None:
        """Called inside the settling transaction, after a child is blocked."""
        assignment = (
            connection.execute(
                text(
                    "SELECT a.root_work_order_id,a.plan_revision,s.profile_id,p.name "
                    "FROM task_session_assignments a "
                    "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.child_session_id "
                    "JOIN profiles p ON p.project_id=s.project_id AND p.id=s.profile_id "
                    "WHERE a.project_id=:p AND a.child_session_id=:child AND a.status='blocked'"
                ),
                {"p": project_id, "child": child_session_id},
            )
            .mappings()
            .one_or_none()
        )
        if assignment is None:
            return
        main = self._main(connection, project_id, assignment["root_work_order_id"])
        detail = f"{assignment['name']}: {reason.replace('_', ' ')}"[:160]
        connection.execute(
            text(
                "INSERT INTO task_session_decision_causes "
                "(id,project_id,actor_id,root_work_order_id,main_session_id,plan_revision,"
                "kind,subject_id,detail) VALUES "
                "(:id,:p,:actor,:root,:main,:revision,'blocker',:subject,:detail) "
                "ON CONFLICT (project_id,root_work_order_id,plan_revision,kind,subject_id) DO NOTHING"
            ),
            {
                "id": uuid4(),
                "p": project_id,
                "actor": f"profile:{assignment['profile_id']}",
                "root": assignment["root_work_order_id"],
                "main": main["id"],
                "revision": assignment["plan_revision"],
                "subject": source_attempt_id,
                "detail": detail,
            },
        )

    def record_synthesis_if_ready(
        self, connection: Any, project_id: UUID, root_order_id: UUID, plan_revision: int
    ) -> None:
        """Wake the Chief when every remaining step has a confirmed result."""
        state = (
            connection.execute(
                text(
                    "SELECT count(*) AS total,count(*) FILTER (WHERE status='satisfied') AS satisfied "
                    "FROM task_session_assignments WHERE project_id=:p AND root_work_order_id=:root "
                    "AND plan_revision=:revision AND status<>'canceled'"
                ),
                {"p": project_id, "root": root_order_id, "revision": plan_revision},
            )
            .mappings()
            .one()
        )
        if not state["total"] or state["total"] != state["satisfied"]:
            return
        # Coalesce new results into an already pending synthesis; a later batch
        # needs a fresh cause only once the earlier checkpoint was dispatched.
        if connection.scalar(
            text(
                "SELECT 1 FROM task_session_decision_causes WHERE project_id=:p "
                "AND root_work_order_id=:root AND plan_revision=:revision AND kind='synthesis' "
                "AND dispatch_attempt_id IS NULL AND resolved_at IS NULL LIMIT 1"
            ),
            {"p": project_id, "root": root_order_id, "revision": plan_revision},
        ):
            return
        main = self._main(connection, project_id, root_order_id)
        subject = connection.scalar(
            text(
                "SELECT a.result_message_id FROM task_session_assignments a "
                "JOIN task_session_messages m ON m.project_id=a.project_id AND m.id=a.result_message_id "
                "WHERE a.project_id=:p AND a.root_work_order_id=:root AND a.plan_revision=:revision "
                "AND a.status='satisfied' ORDER BY m.created_at DESC,m.id DESC LIMIT 1"
            ),
            {"p": project_id, "root": root_order_id, "revision": plan_revision},
        )
        connection.execute(
            text(
                "INSERT INTO task_session_decision_causes "
                "(id,project_id,actor_id,root_work_order_id,main_session_id,plan_revision,"
                "kind,subject_id,detail) VALUES "
                "(:id,:p,:actor,:root,:main,:revision,'synthesis',:subject,:detail) "
                "ON CONFLICT (project_id,root_work_order_id,plan_revision,kind,subject_id) DO NOTHING"
            ),
            {
                "id": uuid4(),
                "p": project_id,
                "actor": self.gateway.actor_id,
                "root": root_order_id,
                "main": main["id"],
                "revision": plan_revision,
                "subject": subject,
                "detail": "All assigned results are saved; review the objective and evidence.",
            },
        )

    @staticmethod
    def resolve_blockers(connection: Any, project_id: UUID, child_session_id: UUID) -> None:
        """A confirmed child result makes its undispatched failed-turn causes stale."""
        connection.execute(
            text(
                "UPDATE task_session_decision_causes c SET resolved_at=clock_timestamp() "
                "FROM task_session_attempts a WHERE c.project_id=:p AND a.project_id=c.project_id "
                "AND a.id=c.subject_id AND a.session_id=:child AND c.kind='blocker' "
                "AND c.dispatch_attempt_id IS NULL AND c.resolved_at IS NULL"
            ),
            {"p": project_id, "child": child_session_id},
        )

    @staticmethod
    def mark_handled(connection: Any, project_id: UUID, chief_session_id: UUID) -> None:
        """A confirmed Chief reply consumed these causes; a stopped turn did not."""
        connection.execute(
            text(
                "UPDATE task_session_decision_causes SET handled_at=clock_timestamp() "
                "WHERE project_id=:p AND main_session_id=:session "
                "AND dispatch_attempt_id IS NOT NULL AND handled_at IS NULL"
            ),
            {"p": project_id, "session": chief_session_id},
        )

    @staticmethod
    def attach_to_attempt(
        connection: Any, project_id: UUID, chief_session_id: UUID, attempt_id: UUID
    ) -> None:
        """Let an operator-started Chief turn consume ready causes already waiting for review."""
        connection.execute(
            text(
                "UPDATE task_session_decision_causes c SET dispatch_attempt_id=:attempt "
                "WHERE c.project_id=:p AND c.main_session_id=:session "
                "AND c.dispatch_attempt_id IS NULL AND c.resolved_at IS NULL "
                "AND (c.kind='blocker' OR NOT EXISTS ("
                "SELECT 1 FROM task_session_assignments a WHERE a.project_id=c.project_id "
                "AND a.root_work_order_id=c.root_work_order_id "
                "AND a.plan_revision=c.plan_revision AND a.status NOT IN ('satisfied','canceled')))"
            ),
            {"p": project_id, "session": chief_session_id, "attempt": attempt_id},
        )

    def dispatch(self, project_id: UUID, root_order_id: UUID) -> dict[str, Any] | None:
        """Queue one Chief turn for all currently actionable causes in one plan revision."""
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            main = self._main(connection, project_id, root_order_id)
            status = connection.scalar(
                text("SELECT status FROM work_orders WHERE project_id=:p AND id=:o"),
                {"p": project_id, "o": root_order_id},
            )
            paused = connection.scalar(
                text("SELECT paused FROM coordination_controls WHERE project_id=:p"),
                {"p": project_id},
            )
            if (
                main["status"] in ("closed", "waiting_for_operator")
                or status not in ("ready", "running")
                or paused
            ):
                return None
            if self.sessions._active_attempt(connection, project_id, main["id"]) is not None:
                return None
            pending = (
                connection.execute(
                    text(
                        "SELECT id,plan_revision,kind FROM task_session_decision_causes "
                        "WHERE project_id=:p AND root_work_order_id=:root "
                        "AND dispatch_attempt_id IS NULL AND resolved_at IS NULL "
                        "ORDER BY plan_revision,created_at,id FOR UPDATE"
                    ),
                    {"p": project_id, "root": root_order_id},
                )
                .mappings()
                .all()
            )
            if not pending:
                return None
            revision = pending[0]["plan_revision"]
            ready = connection.execute(
                text(
                    "SELECT count(*)=count(*) FILTER (WHERE status IN ('satisfied','canceled')) AS ready "
                    "FROM task_session_assignments WHERE project_id=:p AND root_work_order_id=:root "
                    "AND plan_revision=:revision"
                ),
                {"p": project_id, "root": root_order_id, "revision": revision},
            ).scalar()
            causes = [
                row["id"]
                for row in pending
                if row["plan_revision"] == revision and (row["kind"] != "synthesis" or ready)
            ]
            if not causes:
                return None
            automatic = decisions_without_progress(connection, project_id, root_order_id, main["id"])
            if automatic >= TeamLimits.of(self.gateway).chief_decisions_per_order:
                # Further Chief turns need the operator; a reply or Continue resumes them.
                paused = connection.execute(
                    text(
                        "UPDATE task_sessions SET status='waiting_for_operator' WHERE project_id=:p "
                        "AND id=:s AND status<>'waiting_for_operator'"
                    ),
                    {"p": project_id, "s": main["id"]},
                ).rowcount
                if paused:
                    self.gateway._event(
                        connection,
                        project_id,
                        main["work_order_id"],
                        None,
                        "task_session_limit_reached",
                        {"limit": "team_limit_chief_decisions", "session_id": str(main["id"])},
                    )
                return None
            attempt_id = self.sessions._queue_attempt(
                connection,
                {
                    "id": main["id"],
                    "project_id": project_id,
                    "root_work_order_id": root_order_id,
                    "work_order_id": main["work_order_id"],
                },
            )
            connection.execute(
                text(
                    "UPDATE task_session_decision_causes SET dispatch_attempt_id=:attempt "
                    "WHERE project_id=:p AND id=ANY(:ids) AND dispatch_attempt_id IS NULL "
                    "AND resolved_at IS NULL"
                ),
                {"p": project_id, "ids": causes, "attempt": attempt_id},
            )
            self.gateway._event(
                connection,
                project_id,
                main["work_order_id"],
                None,
                "task_session_chief_decision_queued",
                {"attempt_id": str(attempt_id), "plan_revision": revision, "cause_count": len(causes)},
            )
            return {"attempt_id": attempt_id, "cause_ids": causes, "plan_revision": revision}

    def dispatch_next(self, project_id: UUID | None = None) -> dict[str, Any] | None:
        """Queue one ready Chief decision without waking a model in this process."""
        with self.gateway.engine.connect() as connection:
            candidates = (
                connection.execute(
                    text(
                        "SELECT c.project_id,c.root_work_order_id,min(c.created_at) AS oldest "
                        "FROM task_session_decision_causes c "
                        "JOIN task_sessions s ON s.project_id=c.project_id AND s.id=c.main_session_id "
                        "JOIN work_orders w ON w.project_id=c.project_id AND w.id=c.root_work_order_id "
                        "JOIN projects p ON p.id=c.project_id AND p.status='active' "
                        "WHERE c.dispatch_attempt_id IS NULL AND c.resolved_at IS NULL "
                        "AND (CAST(:p AS uuid) IS NULL OR c.project_id=:p) "
                        "AND w.status IN ('ready','running') AND s.execution_mode='team' "
                        "AND s.status NOT IN ('waiting_for_operator','closed') "
                        "AND NOT EXISTS(SELECT 1 FROM coordination_controls controls "
                        "WHERE controls.project_id=c.project_id AND controls.paused) AND NOT EXISTS ("
                        "SELECT 1 FROM task_session_attempts a WHERE a.project_id=c.project_id "
                        "AND a.session_id=c.main_session_id AND a.status IN ('queued','running')) "
                        "AND (c.kind='blocker' OR NOT EXISTS ("
                        "SELECT 1 FROM task_session_assignments x WHERE x.project_id=c.project_id "
                        "AND x.root_work_order_id=c.root_work_order_id "
                        "AND x.plan_revision=c.plan_revision AND x.status NOT IN ('satisfied','canceled'))) "
                        "GROUP BY c.project_id,c.root_work_order_id ORDER BY oldest LIMIT 30"
                    ),
                    {"p": project_id},
                )
                .mappings()
                .all()
            )
        for candidate in candidates:
            result = self.dispatch(candidate["project_id"], candidate["root_work_order_id"])
            if result is not None:
                return result
        return None
