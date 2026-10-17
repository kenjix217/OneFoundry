"""Receipt-backed specialist assignments and deterministic dependency release."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import chief_profile_id, lock_project
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_rework import prior_result_receipt
from onefoundry.task_session_store import TaskSessionStore, TeamLimitReached, TeamLimits
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

AcceptanceKind = Literal["text_report", "artifact", "operator_review", "qa_review"]


class DependencyNotAccepted(Conflict):
    """A rejected or retired prerequisite can never release new work."""

    code = "dependency_not_accepted"

    @staticmethod
    def correction() -> dict[str, str]:
        return {
            "reason": DependencyNotAccepted.code,
            "next_step": (
                "A blocked or canceled assignment cannot be a prerequisite. Read its saved feedback, "
                "put the corrections in the rework objective, and depend on confirmed findings or "
                "new active work. Create a new QA review after the revised result."
            ),
        }


@dataclass(frozen=True)
class DelegateRequest:
    client_action_id: UUID
    profile_id: UUID
    objective: str
    expected_result: str
    acceptance_check: str
    acceptance_kind: AcceptanceKind = "text_report"
    depends_on: tuple[UUID, ...] = ()
    plan_revision: int = 1

    def validated(self) -> DelegateRequest:
        if self.plan_revision < 1 or len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("invalid assignment revision or dependencies")
        for value in (self.objective, self.expected_result, self.acceptance_check):
            if len(value) > 16000:
                raise ValueError("assignment text exceeds the saved limit")
            local_text(value)
        if self.acceptance_kind not in ("text_report", "artifact", "operator_review", "qa_review"):
            raise ValueError("invalid acceptance kind")
        return self


def _payload_hash(parent_session_id: UUID, source_attempt_id: UUID, data: DelegateRequest) -> str:
    value = {
        "parent_session_id": str(parent_session_id),
        "source_attempt_id": str(source_attempt_id),
        "profile_id": str(data.profile_id),
        "objective": data.objective,
        "expected_result": data.expected_result,
        "acceptance_check": data.acceptance_check,
        "acceptance_kind": data.acceptance_kind,
        "depends_on": [str(item) for item in data.depends_on],
        "plan_revision": data.plan_revision,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class TaskSessionAssignments:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        self.sessions = TaskSessionStore(gateway)

    def block_failed_coding(self, project_id: UUID, attempt_id: UUID) -> bool:
        """A failed or explicitly abandoned build blocks its child and informs Chief once."""
        from onefoundry.task_session_decisions import TaskSessionDecisions

        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            row = (
                connection.execute(
                    text(
                        "SELECT a.id,a.child_session_id,r.work_order_id,r.id AS "
                        "run_id,c.reason_code,c.status "
                        "FROM external_code_attempts c JOIN runs r ON r.project_id=c.project_id "
                        "AND r.id=c.run_id JOIN task_sessions s ON s.project_id=r.project_id "
                        "AND s.work_order_id=r.work_order_id JOIN task_session_assignments a "
                        "ON a.project_id=s.project_id AND a.child_session_id=s.id "
                        "WHERE c.project_id=:p AND c.id=:id AND c.fixture_id='repository_code_v1' "
                        "AND (c.status='failed' OR (c.status='unknown' AND EXISTS (SELECT 1 FROM events e "
                        "WHERE e.project_id=c.project_id AND e.event_type='repository_code_abandoned' "
                        "AND e.payload->>'attempt_id'=c.id::text "
                        "AND e.payload->>'request_sha256'=c.request_sha256 "
                        "AND e.payload->>'receipt_sha256'=c.receipt_sha256))) "
                        "AND s.kind='child' AND s.status<>'closed' "
                        "AND a.status IN ('ready','active') FOR UPDATE OF a"
                    ),
                    {"p": project_id, "id": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            abandoned = row["status"] == "unknown"
            reason = (
                "external_coding_abandoned_unverified"
                if abandoned
                else row["reason_code"] or "external_coding_failed"
            )
            connection.execute(
                text("UPDATE task_session_assignments SET status='blocked' WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": row["id"]},
            )
            TaskSessionDecisions(self.gateway).record_blocker(
                connection,
                project_id,
                row["child_session_id"],
                attempt_id,
                reason,
            )
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                row["run_id"],
                "self_extension_coding_abandonment_observed"
                if abandoned
                else "self_extension_coding_failure_observed",
                {
                    "cause_id": str(attempt_id),
                    "assignment_id": str(row["id"]),
                    "reason": reason,
                },
            )
            return True

    def delegate(
        self,
        project_id: UUID,
        parent_session_id: UUID,
        source_attempt_id: UUID,
        data: DelegateRequest,
        *,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        data.validated()
        digest = _payload_hash(parent_session_id, source_attempt_id, data)
        with self.gateway.engine.begin() if connection is None else nullcontext(connection) as connection:
            lock_project(connection, project_id)
            prior = (
                connection.execute(
                    text(
                        "SELECT * FROM task_session_assignments "
                        "WHERE project_id=:p AND client_action_id=:action"
                    ),
                    {"p": project_id, "action": data.client_action_id},
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None:
                if prior["payload_sha256"] != digest:
                    raise Conflict("assignment action identity was used for different work")
                return dict(prior)
            parent = (
                connection.execute(
                    text(
                        "SELECT s.*,p.name AS profile_name,p.kind AS profile_kind,p.allowed_tools "
                        "FROM task_sessions s JOIN profiles p ON p.project_id=s.project_id "
                        "AND p.id=s.profile_id WHERE s.project_id=:p AND s.id=:s FOR UPDATE OF s"
                    ),
                    {"p": project_id, "s": parent_session_id},
                )
                .mappings()
                .one_or_none()
            )
            if parent is None:
                raise NotFound("parent agent session not found")
            if parent["execution_mode"] != "team" or parent["status"] != "working":
                raise Conflict("parent agent session is not working in team mode")
            can_delegate = (
                parent["kind"] == "main" and parent["profile_id"] == chief_profile_id(connection, project_id)
            ) or "delegate_agent_v1" in parent["allowed_tools"]
            if not can_delegate:
                raise Conflict("this agent is not admitted to delegate work")
            attempt = (
                connection.execute(
                    text(
                        "SELECT id FROM task_session_attempts WHERE project_id=:p AND id=:a "
                        "AND session_id=:s AND status='running'"
                    ),
                    {"p": project_id, "a": source_attempt_id, "s": parent_session_id},
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                raise Conflict("delegation is not bound to an active agent turn")
            target = (
                connection.execute(
                    text(
                        "SELECT id,autonomy_level,name,kind FROM profiles WHERE project_id=:p AND id=:profile"
                    ),
                    {"p": project_id, "profile": data.profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise NotFound("target agent profile not found")
            if data.profile_id == parent["profile_id"]:
                raise Conflict("an agent cannot delegate to its own profile")
            if data.acceptance_kind == "qa_review" and (
                target["name"] != "QA" or target["kind"] != "builtin" or not data.depends_on
            ):
                raise Conflict("QA review requires the QA profile and confirmed dependency inputs")
            self._admit_within_limits(connection, project_id, parent, data)
            if data.depends_on:
                dependencies = (
                    connection.execute(
                        text(
                            "SELECT id,status FROM task_session_assignments WHERE project_id=:p "
                            "AND root_work_order_id=:root AND parent_session_id=:parent "
                            "AND (plan_revision=:revision OR status='satisfied') AND id=ANY(:ids)"
                        ),
                        {
                            "p": project_id,
                            "root": parent["root_work_order_id"],
                            "parent": parent_session_id,
                            "revision": data.plan_revision,
                            "ids": list(data.depends_on),
                        },
                    )
                    .mappings()
                    .all()
                )
                if {item["id"] for item in dependencies} != set(data.depends_on):
                    raise Conflict("assignment dependency is outside this plan")
                if any(item["status"] in ("blocked", "canceled") for item in dependencies):
                    raise DependencyNotAccepted("rejected or canceled prerequisites require a new plan")
            waiting = bool(
                data.depends_on
                and connection.scalar(
                    text(
                        "SELECT 1 FROM task_session_assignments WHERE project_id=:p AND id=ANY(:ids) "
                        "AND status<>'satisfied' LIMIT 1"
                    ),
                    {"p": project_id, "ids": list(data.depends_on)},
                )
            )
            child_id, assignment_id, child_order_id = uuid4(), uuid4(), uuid4()
            connection.execute(
                text(
                    "INSERT INTO work_orders (id,project_id,actor_id,title,instructions,profile_id,"
                    "deliverable_kind,acceptance_checks,status,parent_work_order_id) VALUES "
                    "(:id,:p,:actor,:title,:instructions,:profile,:kind,:checks,:order_status,:parent)"
                ).bindparams(bindparam("checks", type_=JSONB)),
                {
                    "id": child_order_id,
                    "p": project_id,
                    "actor": f"profile:{parent['profile_id']}",
                    "title": data.objective[:160],
                    "instructions": data.objective,
                    "profile": data.profile_id,
                    "kind": "artifact" if data.acceptance_kind == "artifact" else "operator_review",
                    "order_status": "draft" if target["autonomy_level"] == "manual" else "ready",
                    "checks": [data.acceptance_check[:300]],
                    "parent": parent["work_order_id"],
                },
            )
            self.gateway._event(
                connection,
                project_id,
                child_order_id,
                None,
                "work_order_created",
                {
                    "source": "team_assignment",
                    "assignment_id": str(assignment_id),
                    "status": "draft" if target["autonomy_level"] == "manual" else "ready",
                    "profile_id": str(data.profile_id),
                },
            )
            child_status = (
                "waiting_for_agent"
                if waiting
                else "waiting_for_operator"
                if target["autonomy_level"] == "manual"
                else "working"
            )
            connection.execute(
                text(
                    "INSERT INTO task_sessions "
                    "(id,project_id,actor_id,work_order_id,root_work_order_id,profile_id,"
                    "parent_session_id,kind,status,execution_mode,is_order_owner) VALUES "
                    "(:id,:p,:actor,:order,:root,:profile,:parent,'child',:status,'team',true)"
                ),
                {
                    "id": child_id,
                    "p": project_id,
                    "actor": f"profile:{parent['profile_id']}",
                    "order": child_order_id,
                    "root": parent["root_work_order_id"],
                    "profile": data.profile_id,
                    "parent": parent_session_id,
                    "status": child_status,
                },
            )
            saved = (
                connection.execute(
                    text(
                        "INSERT INTO task_session_assignments "
                        "(id,project_id,actor_id,root_work_order_id,parent_session_id,child_session_id,"
                        "source_attempt_id,client_action_id,payload_sha256,plan_revision,objective,"
                        "expected_result,acceptance_check,acceptance_kind,status) VALUES "
                        "(:id,:p,:actor,:root,:parent,:child,:attempt,:action,:digest,:revision,"
                        ":objective,:result,:check,:kind,:status) RETURNING *"
                    ),
                    {
                        "id": assignment_id,
                        "p": project_id,
                        "actor": f"profile:{parent['profile_id']}",
                        "root": parent["root_work_order_id"],
                        "parent": parent_session_id,
                        "child": child_id,
                        "attempt": source_attempt_id,
                        "action": data.client_action_id,
                        "digest": digest,
                        "revision": data.plan_revision,
                        "objective": data.objective,
                        "result": data.expected_result,
                        "check": data.acceptance_check,
                        "kind": data.acceptance_kind,
                        "status": "pending" if waiting else "ready",
                    },
                )
                .mappings()
                .one()
            )
            for predecessor in data.depends_on:
                connection.execute(
                    text(
                        "INSERT INTO task_session_dependencies "
                        "(project_id,actor_id,root_work_order_id,assignment_id,depends_on_id) "
                        "VALUES (:p,:actor,:root,:assignment,:dependency)"
                    ),
                    {
                        "p": project_id,
                        "actor": f"profile:{parent['profile_id']}",
                        "root": parent["root_work_order_id"],
                        "assignment": assignment_id,
                        "dependency": predecessor,
                    },
                )
            if not waiting and target["autonomy_level"] != "manual":
                self.sessions._queue_attempt(
                    connection,
                    {
                        "id": child_id,
                        "project_id": project_id,
                        "root_work_order_id": parent["root_work_order_id"],
                        "work_order_id": child_order_id,
                    },
                )
            self.gateway._event(
                connection,
                project_id,
                parent["work_order_id"],
                None,
                "task_session_agent_delegated",
                {
                    "parent_session_id": str(parent_session_id),
                    "child_session_id": str(child_id),
                    "assignment_id": str(assignment_id),
                    "dependencies": [str(item) for item in data.depends_on],
                },
            )
            return dict(saved)

    def _admit_within_limits(
        self, connection: Any, project_id: UUID, parent: Any, data: DelegateRequest
    ) -> None:
        limits = TeamLimits.of(self.gateway)
        if data.plan_revision > limits.plan_revisions:
            raise TeamLimitReached("team_limit_plan_revisions")
        parent_depth = connection.scalar(
            text(
                "WITH RECURSIVE chain(id,parent,depth) AS ("
                "SELECT id,parent_session_id,0 FROM task_sessions WHERE project_id=:p AND id=:s "
                "UNION ALL SELECT t.id,t.parent_session_id,c.depth+1 FROM task_sessions t "
                "JOIN chain c ON t.project_id=:p AND t.id=c.parent WHERE c.depth<10) "
                "SELECT max(depth) FROM chain"
            ),
            {"p": project_id, "s": parent["id"]},
        )
        if int(parent_depth) + 1 > limits.delegation_depth:
            raise TeamLimitReached("team_limit_delegation_depth")
        existing = connection.scalar(
            text(
                "SELECT count(*) FROM task_session_assignments "
                "WHERE project_id=:p AND root_work_order_id=:root AND status NOT IN ('satisfied','canceled')"
            ),
            {"p": project_id, "root": parent["root_work_order_id"]},
        )
        if int(existing) >= limits.assignments_per_order:
            raise TeamLimitReached("team_limit_assignments")

    def report_text_result(
        self,
        project_id: UUID,
        child_session_id: UUID,
        source_attempt_id: UUID,
        client_action_id: UUID,
        body: str,
        *,
        evidence_kind: AcceptanceKind = "text_report",
    ) -> dict[str, Any]:
        result = local_text(body)
        # Artifact envelopes contain exact content already bounded and verified by
        # the artifact owner. The public text-report cap applies to text, not to
        # that derived envelope plus its independently validated summary.
        if evidence_kind != "artifact" and len(result) > 16000:
            raise ValueError("result exceeds the saved message limit")
        digest = hashlib.sha256(result.encode()).hexdigest()
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            prior = (
                connection.execute(
                    text(
                        "SELECT id,child_session_id,result_message_id,result_sha256 FROM "
                        "task_session_assignments WHERE project_id=:p AND result_action_id=:action"
                    ),
                    {"p": project_id, "action": client_action_id},
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None:
                if prior["child_session_id"] != child_session_id or prior["result_sha256"] != digest:
                    raise Conflict("result action identity was used for different work")
                return {
                    "assignment_id": prior["id"],
                    "result_message_id": prior["result_message_id"],
                    "released": [],
                }
            historical = prior_result_receipt(
                connection, project_id, child_session_id, client_action_id, digest
            )
            if historical is not None:
                return historical
            assignment = (
                connection.execute(
                    text(
                        "SELECT a.*,s.work_order_id,s.profile_id,s.execution_mode "
                        "FROM task_session_assignments a JOIN task_sessions s "
                        "ON s.project_id=a.project_id AND s.id=a.child_session_id "
                        "WHERE a.project_id=:p AND a.child_session_id=:child FOR UPDATE OF a,s"
                    ),
                    {"p": project_id, "child": child_session_id},
                )
                .mappings()
                .one_or_none()
            )
            if assignment is None:
                raise NotFound("agent assignment not found")
            if assignment["execution_mode"] != "team" or assignment["status"] not in ("ready", "active"):
                raise Conflict("agent assignment is not active")
            if assignment["acceptance_kind"] != evidence_kind:
                raise Conflict("this assignment requires artifact or operator evidence")
            active = connection.scalar(
                text(
                    "SELECT id FROM task_session_attempts WHERE project_id=:p AND session_id=:child "
                    "AND id=:attempt AND status='running'"
                ),
                {"p": project_id, "child": child_session_id, "attempt": source_attempt_id},
            )
            if active is None:
                raise Conflict("result is not bound to an active agent turn")
            sequence = self.sessions._sequence(connection, project_id, child_session_id) + 1
            result_message_id = uuid4()
            connection.execute(
                text(
                    "INSERT INTO task_session_messages "
                    "(id,project_id,actor_id,root_work_order_id,session_id,sequence,kind,body) "
                    "VALUES (:id,:p,:actor,:root,:child,:sequence,'agent',:body)"
                ),
                {
                    "id": result_message_id,
                    "p": project_id,
                    "actor": f"profile:{assignment['profile_id']}",
                    "root": assignment["root_work_order_id"],
                    "child": child_session_id,
                    "sequence": sequence,
                    "body": result,
                },
            )
            connection.execute(
                text(
                    "UPDATE task_session_assignments SET status='satisfied',"
                    "result_message_id=:message,result_action_id=:action,result_sha256=:digest "
                    "WHERE project_id=:p AND id=:assignment"
                ),
                {
                    "p": project_id,
                    "assignment": assignment["id"],
                    "message": result_message_id,
                    "action": client_action_id,
                    "digest": digest,
                },
            )
            connection.execute(
                text(
                    "UPDATE work_orders SET status='needs_review' WHERE project_id=:p AND id=:o "
                    "AND status IN ('ready','running')"
                ),
                {"p": project_id, "o": assignment["work_order_id"]},
            )
            connection.execute(
                text("UPDATE task_sessions SET status='closed' WHERE project_id=:p AND id=:s"),
                {"p": project_id, "s": child_session_id},
            )
            self.gateway._event(
                connection,
                project_id,
                assignment["work_order_id"],
                None,
                "task_session_result_reported",
                {
                    "assignment_id": str(assignment["id"]),
                    "child_session_id": str(child_session_id),
                    "result_message_id": str(result_message_id),
                },
            )
            released = self._release_ready(connection, project_id, assignment["root_work_order_id"])
            TaskSessionDecisions.resolve_blockers(connection, project_id, child_session_id)
            TaskSessionDecisions(self.gateway).record_synthesis_if_ready(
                connection, project_id, assignment["root_work_order_id"], assignment["plan_revision"]
            )
            return {
                "assignment_id": assignment["id"],
                "result_message_id": result_message_id,
                "released": released,
            }

    def _release_ready(self, connection: Any, project_id: UUID, root_order_id: UUID) -> list[UUID]:
        rows = (
            connection.execute(
                text(
                    "SELECT a.id,a.child_session_id,s.work_order_id,w.status AS order_status "
                    "FROM task_session_assignments a "
                    "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.child_session_id "
                    "JOIN work_orders w ON w.project_id=s.project_id AND w.id=s.work_order_id "
                    "WHERE a.project_id=:p AND a.root_work_order_id=:root AND a.status='pending' "
                    "AND NOT EXISTS (SELECT 1 FROM task_session_dependencies d "
                    "JOIN task_session_assignments previous ON previous.project_id=d.project_id "
                    "AND previous.id=d.depends_on_id WHERE d.project_id=a.project_id "
                    "AND d.assignment_id=a.id AND previous.status<>'satisfied') "
                    "ORDER BY a.created_at,a.id FOR UPDATE OF a"
                ),
                {"p": project_id, "root": root_order_id},
            )
            .mappings()
            .all()
        )
        released: list[UUID] = []
        for row in rows:
            connection.execute(
                text(
                    "UPDATE task_session_assignments SET status='ready' "
                    "WHERE project_id=:p AND id=:assignment"
                ),
                {"p": project_id, "assignment": row["id"]},
            )
            if row["order_status"] == "draft":
                connection.execute(
                    text(
                        "UPDATE task_sessions SET status='waiting_for_operator' WHERE project_id=:p AND id=:s"
                    ),
                    {"p": project_id, "s": row["child_session_id"]},
                )
                continue
            self.sessions._queue_attempt(
                connection,
                {
                    "id": row["child_session_id"],
                    "project_id": project_id,
                    "root_work_order_id": root_order_id,
                    "work_order_id": row["work_order_id"],
                },
            )
            released.append(row["id"])
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                None,
                "task_session_dependency_released",
                {"assignment_id": str(row["id"]), "child_session_id": str(row["child_session_id"])},
            )
        return released
