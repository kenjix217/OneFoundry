"""Scoped, receipt-backed OneFoundry tools for one active external agent turn."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import (
    chief_session_tools,
    self_extension_tools,
    task_session_cancellation,
    task_session_report_contract,
    task_session_results,
    task_session_retry,
)
from onefoundry.chief_inspection_pages import InspectionStateChanged
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.profile_hierarchy import chief_profile_id, lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import (
    ToolDenied,
    ToolDependencyFailure,
    ToolOutputRejected,
    ToolPolicyRejected,
    ToolRepositoryReadFailure,
    ToolTimedOut,
)
from onefoundry.role_web_admission import WebDependencyFailure, WebPolicyFailure, saved_web_correction
from onefoundry.task_session_assignments import DelegateRequest, DependencyNotAccepted, TaskSessionAssignments
from onefoundry.task_session_dependency_context import DependencyPageArgs, read_dependency_page
from onefoundry.task_session_input_pages import InputFilePageArgs, read_input_file_page
from onefoundry.task_session_store import TaskSessionStore, TeamLimitReached
from onefoundry.task_session_team_context import team_roster
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

NAMESPACE = "onefoundry"
PUBLISH = f"{NAMESPACE}.publish_update"
DELEGATE = f"{NAMESPACE}.delegate_agent"
REPORT = f"{NAMESPACE}.report_result"
ROLE = f"{NAMESPACE}.role_tool"
COMPLETE = f"{NAMESPACE}.submit_completion"
CANCEL = f"{NAMESPACE}.cancel_assignment"
RETRY = f"{NAMESPACE}.retry_assignment"
READ_DEPENDENCY = f"{NAMESPACE}.read_dependency_result"
READ_INPUT_FILE = f"{NAMESPACE}.read_input_file"
LIST_AGENTS = f"{NAMESPACE}.list_agents"
READ_ONLY = chief_session_tools.CHIEF_READS | {
    READ_DEPENDENCY,
    READ_INPUT_FILE,
    LIST_AGENTS,
    self_extension_tools.READ,
    self_extension_tools.INSPECT,
}


class PublishUpdateArgs(StrictModel):
    body: str = Field(min_length=1, max_length=16000)


class DelegateAgentArgs(StrictModel):
    profile_id: UUID
    objective: str = Field(min_length=1, max_length=16000)
    expected_result: str = Field(min_length=1, max_length=16000)
    acceptance_check: str = Field(min_length=1, max_length=16000)
    depends_on: list[UUID] = Field(default_factory=list)
    plan_revision: int = Field(default=1, ge=1)
    acceptance_kind: str = "text_report"


class AgentPageArgs(StrictModel):
    offset: int = Field(default=0, ge=0)


class ReportResultArgs(StrictModel):
    body: str = Field(min_length=1, max_length=16000)


def _response(success: bool, content: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "success": success,
        "contentItems": [{"type": "inputText", "text": json.dumps(content, sort_keys=True)}],
    }


class TaskSessionTools:
    """Gateway side of Codex dynamic tools; the app-server adapter owns turn scope."""

    def __init__(self, gateway: ToolGateway, project_id: UUID, session_id: UUID, attempt_id: UUID) -> None:
        self.gateway = gateway
        self.project_id = project_id
        self.session_id = session_id
        self.attempt_id = attempt_id
        self.sessions = TaskSessionStore(gateway)
        self.assignments = TaskSessionAssignments(gateway)

    def _scope(self) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT s.id,s.root_work_order_id,s.work_order_id,s.profile_id,s.kind,"
                        "p.kind AS profile_kind,p.name AS profile_name,p.allowed_tools,"
                        "w.status AS order_status,root.status AS root_status,a.purpose "
                        "FROM task_session_attempts a JOIN task_sessions s "
                        "ON s.project_id=a.project_id AND s.id=a.session_id "
                        "JOIN profiles p ON p.project_id=s.project_id AND p.id=s.profile_id "
                        "JOIN work_orders w ON w.project_id=s.project_id AND w.id=s.work_order_id "
                        "JOIN work_orders root ON root.project_id=s.project_id "
                        "AND root.id=s.root_work_order_id "
                        "WHERE a.project_id=:p AND a.id=:attempt AND a.session_id=:session "
                        "AND a.status='running' AND s.execution_mode='team'"
                    ),
                    {"p": self.project_id, "attempt": self.attempt_id, "session": self.session_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise Conflict("team tool call is outside an active agent turn")
            scope = dict(row)
            scope["is_project_chief"] = scope["profile_id"] == chief_profile_id(connection, self.project_id)
            scope["actionable"] = (
                scope["purpose"] == "work"
                and scope["order_status"] in ("ready", "running")
                and scope["root_status"]
                in (
                    "ready",
                    "running",
                )
            )
            assignment = (
                connection.execute(
                    text(
                        "SELECT acceptance_kind,status FROM task_session_assignments "
                        "WHERE project_id=:p AND child_session_id=:session"
                    ),
                    {"p": self.project_id, "session": self.session_id},
                )
                .mappings()
                .one_or_none()
            )
            scope["assignment"] = dict(assignment) if assignment is not None else None
            bound = connection.scalar(
                text("SELECT 1 FROM runs WHERE project_id=:p AND id=:a AND status='running'"),
                {"p": self.project_id, "a": self.attempt_id},
            )
            scope["role_tools"] = (
                self.gateway.available_role_tools(self.project_id, self.attempt_id, scope["profile_id"])
                if bound is not None and scope["actionable"]
                else []
            )
            scope["has_blocked"] = bool(
                connection.scalar(
                    text(
                        "SELECT 1 FROM task_session_assignments WHERE project_id=:p "
                        "AND parent_session_id=:s AND status='blocked' LIMIT 1"
                    ),
                    {"p": self.project_id, "s": self.session_id},
                )
            )
            scope["can_complete"] = bool(
                connection.scalar(
                    text(
                        "SELECT count(*)>0 AND bool_and(status IN ('satisfied','canceled')) "
                        "FROM task_session_assignments WHERE project_id=:p AND parent_session_id=:s"
                    ),
                    {"p": self.project_id, "s": self.session_id},
                )
            )
            return scope

    @staticmethod
    def _admitted(scope: Mapping[str, Any]) -> frozenset[str]:
        tools = {PUBLISH} | self_extension_tools.admitted(scope)
        chief = scope["kind"] == "main" and scope["is_project_chief"]
        if chief:
            tools.update(chief_session_tools.CHIEF_READS)
            tools.add(LIST_AGENTS)
        if not scope.get("actionable", True):
            return frozenset(tools)
        tools.update((READ_DEPENDENCY, READ_INPUT_FILE, LIST_AGENTS))
        if chief or "delegate_agent_v1" in scope["allowed_tools"]:
            tools.add(DELEGATE)
        if chief and scope.get("has_blocked"):
            tools.update((CANCEL, RETRY))
        if chief and scope.get("can_complete"):
            tools.add(COMPLETE)
        if scope.get("role_tools"):
            tools.add(ROLE)
        assignment = scope["assignment"]
        if (
            assignment
            and assignment["status"] in ("ready", "active")
            and assignment["acceptance_kind"] in ("text_report", "artifact", "qa_review")
        ):
            tools.add(REPORT)
        return frozenset(tools)

    def specifications(self) -> list[dict[str, Any]]:
        scope = self._scope()
        admitted = self._admitted(scope)
        specs: list[dict[str, Any]] = self_extension_tools.specifications(admitted)
        if chief_session_tools.READ_PROJECT in admitted:
            specs.extend(chief_session_tools.specifications())
        if READ_DEPENDENCY in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "read_dependency_result",
                    "description": (
                        "Read an exact page or availability of a declared dependency or Chief-owned "
                        "assignment. Pending means finish this turn so queued specialists can run; "
                        "do not poll within this turn. Blocked QA reads return saved review feedback, "
                        "not an accepted result. Read all pages before evaluating omitted content. "
                        "Offsets are characters; preserve message ID and digest."
                    ),
                    "inputSchema": DependencyPageArgs.model_json_schema(),
                }
            )
        if READ_INPUT_FILE in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "read_input_file",
                    "description": (
                        "Read an exact bounded page of a ready text file already attached to this project "
                        "or root work order. Use the file IDs in the task input index. Read pages until "
                        "next_offset is null before evaluating omitted content. Offsets are characters; "
                        "original_utf8 preserves the uploaded .txt bytes and trailing newline, while "
                        "extracted_sections identifies transformed text."
                    ),
                    "inputSchema": InputFilePageArgs.model_json_schema(),
                }
            )
        if LIST_AGENTS in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "list_agents",
                    "description": (
                        "Read the next bounded page of project profile metadata. This grants no tools; "
                        "only unblocked profiles may be delegated."
                    ),
                    "inputSchema": AgentPageArgs.model_json_schema(),
                }
            )
        if PUBLISH in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "publish_update",
                    "description": "Save a public plan, finding, blocker, or next step.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"body": {"type": "string"}},
                        "required": ["body"],
                        "additionalProperties": False,
                    },
                }
            )
        if DELEGATE in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "delegate_agent",
                    "description": (
                        "Assign a bounded text, artifact, or QA task to an agent. "
                        "Use the returned IDs for dependencies."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "profile_id": {"type": "string"},
                            "objective": {"type": "string"},
                            "expected_result": {"type": "string"},
                            "acceptance_check": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "plan_revision": {"type": "integer", "minimum": 1},
                            "acceptance_kind": {
                                "type": "string",
                                "enum": ["text_report", "artifact", "qa_review"],
                            },
                        },
                        "required": ["profile_id", "objective", "expected_result", "acceptance_check"],
                        "additionalProperties": False,
                    },
                }
            )
        if REPORT in admitted:
            specs.append(task_session_report_contract.specification(scope["assignment"]["acceptance_kind"]))
        if ROLE in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "role_tool",
                    "description": "Invoke one admitted role tool with its exact contract: "
                    + json.dumps(scope["role_tools"], sort_keys=True),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_id": {
                                "type": "string",
                                "enum": [item["id"] for item in scope["role_tools"]],
                            },
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool_id", "arguments"],
                        "additionalProperties": False,
                    },
                }
            )
        if RETRY in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "retry_assignment",
                    "description": "Safely retry an owned blocked assignment without an accepted result. "
                    "Preserve prior receipts. Unknown effects, QA rejection and configured "
                    "bounds stop retries.",
                    "inputSchema": task_session_retry.RetryAssignment.model_json_schema(),
                }
            )
        if CANCEL in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "cancel_assignment",
                    "description": (
                        "Cancel a blocked branch before rework. "
                        "Active or unknown attempts need operator reconciliation."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {"assignment_id": {"type": "string"}},
                        "required": ["assignment_id"],
                        "additionalProperties": False,
                    },
                }
            )
        if COMPLETE in admitted:
            specs.append(
                {
                    "type": "function",
                    "name": "submit_completion",
                    "description": (
                        "Submit verified accepted work and independent QA. New Chief-owned "
                        "local jobs complete "
                        "under their saved chief_verified policy; existing operator_review jobs await the "
                        "operator. Outbound and coding completion retain their separate owners."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "body": {"type": "string"},
                            "artifact_id": {"type": "string"},
                            "artifact_run_id": {"type": "string"},
                        },
                        "required": ["body"],
                        "additionalProperties": False,
                    },
                }
            )
        return [
            {
                "type": "namespace",
                "name": NAMESPACE,
                "description": "Saved OneFoundry work-order actions",
                "tools": specs,
            }
        ]

    def reconcile_previous_actions(self, *, operator: bool = False) -> int:
        """Settle interrupted local tool receipts from their authoritative rows."""
        with self.gateway.engine.begin() as connection:
            lock_project(connection, self.project_id)
            active = connection.scalar(
                text(
                    "SELECT id FROM task_session_attempts WHERE project_id=:p AND id=:a "
                    "AND session_id=:s AND status='running'"
                ),
                {"p": self.project_id, "a": self.attempt_id, "s": self.session_id},
            )
            if operator:
                busy = connection.scalar(
                    text(
                        "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND session_id=:s "
                        "AND status IN ('queued','running') LIMIT 1"
                    ),
                    {"p": self.project_id, "s": self.session_id},
                )
                if busy is not None:
                    raise Conflict("wait for queued or running work before reconciling saved actions")
            elif active is None:
                raise Conflict("team action reconciliation requires an active continuation")
            work_order_id = connection.scalar(
                text(
                    "SELECT work_order_id FROM task_sessions WHERE project_id=:p AND id=:s "
                    "AND execution_mode='team'"
                ),
                {"p": self.project_id, "s": self.session_id},
            )
            if work_order_id is None:
                raise NotFound("team session not found")
            receipts = connection.execute(
                text(
                    "SELECT c.id,c.tool_name FROM task_session_tool_calls c "
                    "JOIN task_session_attempts a ON a.project_id=c.project_id AND a.id=c.attempt_id "
                    "WHERE c.project_id=:p AND c.session_id=:s AND c.attempt_id<>:current "
                    "AND c.status='started' AND a.status IN ('done','failed','unknown') "
                    "ORDER BY c.created_at,c.id FOR UPDATE OF c"
                ),
                {"p": self.project_id, "s": self.session_id, "current": self.attempt_id},
            ).mappings()
            count = 0
            for receipt in receipts:
                result = self._readback_action(connection, receipt["id"], receipt["tool_name"])
                reason = (
                    None
                    if result is not None
                    else ("read_interrupted" if receipt["tool_name"] in READ_ONLY else "effect_not_found")
                )
                connection.execute(
                    text(
                        "UPDATE task_session_tool_calls SET status=:status,result=:result,reason=:reason,"
                        "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id AND status='started'"
                    ).bindparams(bindparam("result", type_=JSONB(none_as_null=True))),
                    {
                        "p": self.project_id,
                        "id": receipt["id"],
                        "status": "done" if result is not None else "failed",
                        "result": result,
                        "reason": reason,
                    },
                )
                self.gateway._event(
                    connection,
                    self.project_id,
                    work_order_id,
                    None,
                    "task_session_tool_reconciled",
                    {"tool_call_id": str(receipt["id"]), "outcome": "done" if result else "failed"},
                )
                count += 1
            return count

    def _readback_action(self, connection: Any, receipt_id: UUID, name: str) -> dict[str, Any] | None:
        if name in READ_ONLY:
            # Reads have no effect to replay or infer. Preserve the interrupted
            # observation as a failed receipt and let a new call read fresh state.
            return None
        if name in self_extension_tools.TOOLS:
            return self_extension_tools.readback(self, connection, receipt_id, name)
        if name in (COMPLETE, CANCEL, REPORT, RETRY):
            recorded = connection.execute(
                text(
                    "SELECT payload FROM events WHERE project_id=:p AND payload->>'tool_call_id'=:id "
                    "AND event_type IN "
                    "('task_session_completion_submitted','task_session_branch_canceled',"
                    "'task_session_qa_review','task_session_assignment_retried') "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"p": self.project_id, "id": str(receipt_id)},
            ).scalar()
            if recorded is not None:
                return dict(recorded)
        if name == ROLE:
            artifact = (
                connection.execute(
                    text(
                        "SELECT id,run_id,kind,mime_type,sha256,byte_size FROM workspace_artifacts WHERE "
                        "project_id=:p "
                        "AND call_id=:call AND status='ready'"
                    ),
                    {"p": self.project_id, "call": receipt_id.hex},
                )
                .mappings()
                .one_or_none()
            )
            if artifact is not None:
                return {
                    "status": "done",
                    "run_id": str(artifact["run_id"]),
                    "result": {
                        **dict(artifact),
                        "run_id": str(artifact["run_id"]),
                        "artifact_id": str(artifact["id"]),
                        "id": str(artifact["id"]),
                    },
                }
            receipt = (
                connection.execute(
                    text(
                        "SELECT id,payload FROM events WHERE project_id=:p AND "
                        "event_type='role_tool_finished' "
                        "AND payload->>'call_id'=:call AND payload->>'status'='done' ORDER BY "
                        "created_at LIMIT 1"
                    ),
                    {"p": self.project_id, "call": receipt_id.hex},
                )
                .mappings()
                .one_or_none()
            )
            return (
                {"status": "done", "owner_event_id": str(receipt["id"]), "readback": dict(receipt["payload"])}
                if receipt
                else None
            )
        if name == PUBLISH:
            message_id = connection.scalar(
                text(
                    "SELECT id FROM task_session_messages WHERE project_id=:p AND session_id=:s "
                    "AND client_message_id=:action"
                ),
                {"p": self.project_id, "s": self.session_id, "action": receipt_id},
            )
            return {"message_id": str(message_id)} if message_id is not None else None
        if name == DELEGATE:
            row = (
                connection.execute(
                    text(
                        "SELECT id,child_session_id,status FROM task_session_assignments "
                        "WHERE project_id=:p AND parent_session_id=:s AND client_action_id=:action"
                    ),
                    {"p": self.project_id, "s": self.session_id, "action": receipt_id},
                )
                .mappings()
                .one_or_none()
            )
            return (
                {
                    "assignment_id": str(row["id"]),
                    "child_session_id": str(row["child_session_id"]),
                    "status": row["status"],
                }
                if row is not None
                else None
            )
        if name == REPORT:
            row = (
                connection.execute(
                    text(
                        "SELECT a.id,a.result_message_id,a.acceptance_kind,m.body "
                        "FROM task_session_assignments a "
                        "JOIN task_session_messages m "
                        "ON m.project_id=a.project_id AND m.id=a.result_message_id "
                        "WHERE a.project_id=:p AND a.child_session_id=:s AND a.result_action_id=:action"
                    ),
                    {"p": self.project_id, "s": self.session_id, "action": receipt_id},
                )
                .mappings()
                .one_or_none()
            )
            return (
                {
                    "assignment_id": str(row["id"]),
                    "result_message_id": str(row["result_message_id"]),
                    "status": "saved",
                    "reviewed_artifacts": (
                        json.loads(row["body"]).get("reviewed_artifacts", [])
                        if row["acceptance_kind"] == "qa_review"
                        else []
                    ),
                }
                if row is not None
                else None
            )
        raise Conflict("team action readback is not supported")

    def _begin(
        self, scope: Mapping[str, Any], name: str, call_id: str, digest: str, *, allow_new: bool
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, self.project_id)
            active = connection.scalar(
                text(
                    "SELECT id FROM task_session_attempts WHERE project_id=:p AND id=:attempt "
                    "AND session_id=:session AND status='running' FOR UPDATE"
                ),
                {"p": self.project_id, "attempt": self.attempt_id, "session": self.session_id},
            )
            if active is None:
                raise Conflict("team tool call is outside an active agent turn")
            prior = (
                connection.execute(
                    text(
                        "SELECT * FROM task_session_tool_calls WHERE project_id=:p "
                        "AND attempt_id=:attempt AND provider_call_id=:call FOR UPDATE"
                    ),
                    {"p": self.project_id, "attempt": self.attempt_id, "call": call_id},
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None:
                if (
                    prior["session_id"] != self.session_id
                    or prior["tool_name"] != name
                    or prior["arguments_sha256"] != digest
                ):
                    raise Conflict("provider tool call identity changed")
                return dict(prior)
            if not allow_new:
                raise Conflict("team tool is not admitted for this agent")
            receipt_id = uuid5(NAMESPACE_URL, f"onefoundry:{self.project_id}:{self.attempt_id}:{call_id}")
            row = (
                connection.execute(
                    text(
                        "INSERT INTO task_session_tool_calls "
                        "(id,project_id,actor_id,root_work_order_id,session_id,attempt_id,"
                        "provider_call_id,tool_name,arguments_sha256) VALUES "
                        "(:id,:p,:actor,:root,:session,:attempt,:call,:name,:digest) RETURNING *"
                    ),
                    {
                        "id": receipt_id,
                        "p": self.project_id,
                        "actor": f"profile:{scope['profile_id']}",
                        "root": scope["root_work_order_id"],
                        "session": self.session_id,
                        "attempt": self.attempt_id,
                        "call": call_id,
                        "name": name,
                        "digest": digest,
                    },
                )
                .mappings()
                .one()
            )
            self.gateway._event(
                connection,
                self.project_id,
                scope["work_order_id"],
                None,
                "task_session_tool_started",
                {
                    "session_id": str(self.session_id),
                    "attempt_id": str(self.attempt_id),
                    "tool_call_id": str(receipt_id),
                    "tool_name": name,
                },
            )
            return dict(row)

    def _finish(
        self,
        scope: Mapping[str, Any],
        receipt_id: UUID,
        *,
        result: dict[str, Any] | None,
        reason: str | None = None,
    ) -> None:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, self.project_id)
            row = connection.execute(
                text(
                    "UPDATE task_session_tool_calls SET status=:status,result=:result,reason=:reason,"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id "
                    "AND status='started' RETURNING id"
                ).bindparams(bindparam("result", type_=JSONB(none_as_null=True))),
                {
                    "p": self.project_id,
                    "id": receipt_id,
                    "status": "done" if result is not None else "failed",
                    "result": result,
                    "reason": reason,
                },
            ).scalar()
            if row is None:
                raise Conflict("team tool call outcome changed")
            self.gateway._event(
                connection,
                self.project_id,
                scope["work_order_id"],
                None,
                "task_session_tool_finished",
                {
                    "session_id": str(self.session_id),
                    "attempt_id": str(self.attempt_id),
                    "tool_call_id": str(receipt_id),
                    "status": "done" if result is not None else "failed",
                    "reason": reason,
                },
            )

    def _publish(self, scope: Mapping[str, Any], receipt_id: UUID, body: str) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            return self._publish_in_transaction(connection, scope, receipt_id, body)

    def _publish_in_transaction(
        self, connection: Any, scope: Mapping[str, Any], receipt_id: UUID, body: str
    ) -> dict[str, Any]:
        body = local_text(body)
        if contains_sensitive_text(body):
            raise ValueError("sensitive update")
        lock_project(connection, self.project_id)
        prior = (
            connection.execute(
                text(
                    "SELECT id,session_id,body FROM task_session_messages "
                    "WHERE project_id=:p AND client_message_id=:client"
                ),
                {"p": self.project_id, "client": receipt_id},
            )
            .mappings()
            .one_or_none()
        )
        if prior is not None:
            if prior["session_id"] != self.session_id or prior["body"] != body:
                raise Conflict("public update identity changed")
            return {"message_id": str(prior["id"])}
        active = connection.scalar(
            text(
                "SELECT id FROM task_session_attempts WHERE project_id=:p AND id=:attempt "
                "AND session_id=:session AND status='running'"
            ),
            {"p": self.project_id, "attempt": self.attempt_id, "session": self.session_id},
        )
        if active is None:
            raise Conflict("public update is outside an active agent turn")
        sequence = self.sessions._sequence(connection, self.project_id, self.session_id) + 1
        message_id = uuid5(NAMESPACE_URL, f"onefoundry:message:{receipt_id}")
        connection.execute(
            text(
                "INSERT INTO task_session_messages "
                "(id,project_id,actor_id,root_work_order_id,session_id,client_message_id,"
                "sequence,kind,body) VALUES "
                "(:id,:p,:actor,:root,:session,:client,:sequence,'agent',:body)"
            ),
            {
                "id": message_id,
                "p": self.project_id,
                "actor": f"profile:{scope['profile_id']}",
                "root": scope["root_work_order_id"],
                "session": self.session_id,
                "client": receipt_id,
                "sequence": sequence,
                "body": body,
            },
        )
        self.gateway._event(
            connection,
            self.project_id,
            scope["work_order_id"],
            None,
            "task_session_update_published",
            {"session_id": str(self.session_id), "message_id": str(message_id)},
        )
        return {"message_id": str(message_id)}

    def _execute(
        self, scope: Mapping[str, Any], name: str, arguments: Mapping[str, Any], receipt_id: UUID
    ) -> dict[str, Any]:
        if name in self_extension_tools.TOOLS:
            return self_extension_tools.execute(self, scope, name, arguments, receipt_id)
        if name in chief_session_tools.CHIEF_READS:
            return chief_session_tools.execute(
                self.gateway, self.project_id, self.session_id, name, arguments, receipt_id
            )
        if name in (READ_DEPENDENCY, READ_INPUT_FILE, LIST_AGENTS):
            settings = self.gateway.live_text_settings
            if settings is None:
                raise Unavailable("team settings unavailable")
            with self.gateway.engine.connect() as connection:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                if name == READ_DEPENDENCY:
                    page_budget = (
                        settings.onefoundry_max_context_bytes
                        if settings.onefoundry_max_context_bytes is not None
                        else settings.live_text_max_prompt_bytes
                    )
                    return read_dependency_page(
                        connection,
                        self.project_id,
                        self.session_id,
                        DependencyPageArgs.model_validate(arguments),
                        page_budget // 2,
                    )
                if name == READ_INPUT_FILE:
                    page_budget = (
                        settings.onefoundry_max_context_bytes
                        if settings.onefoundry_max_context_bytes is not None
                        else settings.live_text_max_prompt_bytes
                    )
                    return read_input_file_page(
                        connection,
                        settings.input_file_root,
                        self.project_id,
                        scope["root_work_order_id"],
                        InputFilePageArgs.model_validate(arguments),
                        page_budget // 2,
                    )
                offset = AgentPageArgs.model_validate(arguments).offset
                agents = team_roster(connection, self.project_id, settings, offset)
                notice = agents.pop() if agents and "omitted_agents" in agents[-1] else None
                return {"agents": agents, "next_offset": notice["next_offset"] if notice else None}
        if name == PUBLISH:
            update_args = PublishUpdateArgs.model_validate(arguments)
            return self._publish(scope, receipt_id, update_args.body)
        if name == DELEGATE:
            delegate_args = DelegateAgentArgs.model_validate(arguments)
            if any(
                contains_sensitive_text(value)
                for value in (
                    delegate_args.objective,
                    delegate_args.expected_result,
                    delegate_args.acceptance_check,
                )
            ):
                raise ValueError("sensitive assignment")
            saved = self.assignments.delegate(
                self.project_id,
                self.session_id,
                self.attempt_id,
                DelegateRequest(
                    client_action_id=receipt_id,
                    profile_id=delegate_args.profile_id,
                    objective=delegate_args.objective,
                    expected_result=delegate_args.expected_result,
                    acceptance_check=delegate_args.acceptance_check,
                    acceptance_kind=cast(Any, delegate_args.acceptance_kind),
                    depends_on=tuple(delegate_args.depends_on),
                    plan_revision=delegate_args.plan_revision,
                ),
            )
            return {
                "assignment_id": str(saved["id"]),
                "child_session_id": str(saved["child_session_id"]),
                "status": saved["status"],
            }
        if name == REPORT:
            return task_session_results.report(
                self,
                scope,
                receipt_id,
                task_session_report_contract.validate(scope["assignment"]["acceptance_kind"], arguments),
            )
        if name == COMPLETE:
            return task_session_results.submit_completion(
                self, scope, receipt_id, task_session_results.CompletionArgs.model_validate(arguments)
            )
        if name == RETRY:
            return task_session_retry.retry(
                self, scope, receipt_id, task_session_retry.RetryAssignment.model_validate(arguments)
            )
        if name == CANCEL:
            if set(arguments) != {"assignment_id"}:
                raise ValueError("invalid cancellation")
            return task_session_cancellation.cancel(
                self, scope, UUID(str(arguments["assignment_id"])), receipt_id
            )
        if name == ROLE:
            if set(arguments) != {"tool_id", "arguments"}:
                raise ValueError("invalid role tool request")
            tool_id = str(arguments["tool_id"])
            descriptor = next((item for item in scope["role_tools"] if item["id"] == tool_id), None)
            if descriptor is None:
                raise Conflict("role tool is not admitted for this agent")
            result = self.gateway.invoke_role_tool(
                self.project_id,
                self.attempt_id,
                scope["profile_id"],
                receipt_id.hex,
                tool_id,
                arguments["arguments"],
                tool_version=descriptor["version"],
            )
            return {**result, "run_id": str(self.attempt_id)}
        raise Conflict("team tool is not admitted")

    def invoke(self, name: str, arguments: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        """Return scoped, bounded receipts; never expose raw errors."""
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 200 or any(ord(c) < 32 for c in call_id):
            raise ValueError("invalid provider call identity")
        if not isinstance(arguments, Mapping):
            raise ValueError("invalid provider tool arguments")
        scope = self._scope()
        try:
            digest = hashlib.sha256(
                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid provider tool arguments") from exc
        prior = self._begin(scope, name, call_id, digest, allow_new=name in self._admitted(scope))
        if prior["status"] == "done":
            return _response(True, prior["result"])
        if prior["status"] == "failed":
            repository_correction = (
                ToolRepositoryReadFailure.saved_correction(prior["reason"], str(arguments.get("tool_id", "")))
                if name == ROLE
                else None
            )
            web_correction = (
                saved_web_correction(prior["reason"], str(arguments.get("tool_id", "")))
                if name == ROLE
                else None
            )
            correction = (
                InspectionStateChanged.correction()
                if prior["reason"] == InspectionStateChanged.code
                else DependencyNotAccepted.correction()
                if prior["reason"] == DependencyNotAccepted.code
                else repository_correction
                or web_correction
                or task_session_report_contract.correction(prior["reason"])
            )
            return _response(False, correction)
        try:
            result = self._execute(scope, name, arguments, prior["id"])
        except task_session_report_contract.ReportInputError as exc:
            self._finish(scope, prior["id"], result=None, reason=exc.code)
            return _response(False, task_session_report_contract.correction(exc.code))
        except (ValidationError, ValueError):
            self._finish(scope, prior["id"], result=None, reason="invalid_tool_input")
            return _response(False, {"reason": "invalid_tool_input"})
        except TeamLimitReached as exc:
            # The operator decides whether the team may go further.
            self._finish(scope, prior["id"], result=None, reason=exc.code)
            return _response(False, {"reason": exc.code, "next_step": "ask_operator"})
        except DependencyNotAccepted as exc:
            correction = exc.correction()
            self._finish(scope, prior["id"], result=None, reason=exc.code)
            return _response(False, correction)
        except InspectionStateChanged as exc:
            self._finish(scope, prior["id"], result=None, reason=exc.code)
            return _response(False, exc.correction())
        except ToolRepositoryReadFailure as exc:
            self._finish(scope, prior["id"], result=None, reason=exc.code)
            return _response(False, exc.as_correction())
        except (WebDependencyFailure, WebPolicyFailure) as exc:
            self._finish(scope, prior["id"], result=None, reason=exc.code)
            return _response(False, exc.as_correction())
        except (ToolDependencyFailure, ToolOutputRejected, ToolPolicyRejected, ToolTimedOut) as exc:
            reason = (
                "tool_dependency_failure"
                if isinstance(exc, ToolDependencyFailure)
                else "tool_output_rejected"
                if isinstance(exc, ToolOutputRejected)
                else "tool_policy_rejected"
                if isinstance(exc, ToolPolicyRejected)
                else "tool_timeout"
            )
            self._finish(scope, prior["id"], result=None, reason=reason)
            return _response(False, {"reason": reason, "next_step": "inspect_capability"})
        except (Conflict, NotFound, Unavailable) as exc:
            reason = (
                "tool_conflict"
                if isinstance(exc, Conflict)
                else ("tool_target_missing" if isinstance(exc, NotFound) else "tool_unavailable")
            )
            self._finish(scope, prior["id"], result=None, reason=reason)
            corrections = {
                "source_not_searched": "Use public_web_search_v1 in this same run, then fetch an exact "
                "returned public HTTPS URL. An earlier run's search does not authorize this fetch.",
                "invalid_arguments": "Use the advertised JSON schema. Public search accepts only a query "
                "of public words, without URLs, domains or email addresses. Page fetch accepts only a url "
                "with a public HTTPS address returned by search in this run.",
                "not_admitted": "Inspect this profile's current capability, grants and runtime settings "
                "before proposing setup. Do not repeat a denied call or build a duplicate tool.",
                "version_mismatch": "Inspect the registered tool descriptor and use its current version.",
            }
            if isinstance(exc, ToolDenied) and exc.reason in corrections:
                return _response(
                    False,
                    {
                        "reason": reason,
                        "owner_reason": exc.reason,
                        "next_step": corrections[exc.reason],
                    },
                )
            return _response(False, {"reason": reason})
        self._finish(scope, prior["id"], result=result)
        return _response(True, result)
