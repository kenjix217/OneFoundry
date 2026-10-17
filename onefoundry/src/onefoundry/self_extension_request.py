"""Chief-owned capability gaps linked to existing specialist assignments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.capability_schemas import CapabilityReport
from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.repository_code_gateway import approved_target, current_source
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.task_execution_capabilities import execution_capabilities
from onefoundry.task_session_assignments import DelegateRequest
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools


class ExtensionRequest(StrictModel):
    operation: str = Field(min_length=1, max_length=80)
    gap_source_id: UUID | None = None
    gap_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    target_approval_id: UUID
    objective: str = Field(min_length=1, max_length=4000)
    acceptance_check: str = Field(min_length=1, max_length=2000)
    checkpoint: str = Field(min_length=1, max_length=4000)

    @field_validator("objective", "acceptance_check", "checkpoint")
    @classmethod
    def safe_text(cls, value: str) -> str:
        local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("extension text appears sensitive")
        return value


def request(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, data: ExtensionRequest
) -> dict[str, Any]:
    gateway = tools.gateway
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("capability inspection configuration unavailable")
    with gateway.engine.connect() as connection:
        target = approved_target(connection, tools.project_id, data.target_approval_id)
        if target.task_scope_id != scope["root_work_order_id"]:
            raise Conflict("maintenance target must be approved for this original task")
        current_source(connection, tools.project_id, target)
        report = CapabilityReport.model_validate(
            execution_capabilities(
                connection, tools.project_id, settings, profile_id=scope["profile_id"], gateway=gateway
            )["readiness"]
        )
        checks = [
            check
            for check in report.checks
            if check.operation == data.operation
            and check.source_id == data.gap_source_id
            and check.revision == data.gap_revision
        ]
        if data.operation.startswith("tool:") and data.gap_source_id is None and data.gap_revision is None:
            from onefoundry.requested_capability import inspect

            checks = [inspect(gateway, tools.project_id, scope["profile_id"], data.operation[5:])]
        if (
            len(checks) != 1
            or checks[0].status != "missing_implementation"
            or any(blocker.kind != "missing_implementation" for blocker in checks[0].blockers)
        ):
            raise Conflict("only a freshly verified implementation gap can create extension work")
        engineer = connection.scalar(
            text("SELECT id FROM profiles WHERE project_id=:p AND kind='builtin' AND name='Engineer'"),
            {"p": tools.project_id},
        )
        if engineer is None:
            raise Conflict("built-in Engineer is unavailable")
    gap_key = uuid5(
        NAMESPACE_URL,
        f"onefoundry:extension:{tools.project_id}:{data.operation}:{data.gap_source_id}:{data.gap_revision}:{target.source_id}:{target.revision}:{target.task_scope_id}:{data.target_approval_id}",
    )
    # Delegate's existing action identity is the crash-safe deduplication owner.
    # Bind deduplication and the one-attempt approval to this original task.
    with gateway.engine.begin() as connection:
        from onefoundry.profile_hierarchy import lock_project

        lock_project(connection, tools.project_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='self_extension_requested' "
                "AND payload->>'extension_key'=:key AND payload->>'original_session_id'=:session LIMIT 1"
            ),
            {"p": tools.project_id, "key": str(gap_key), "session": str(tools.session_id)},
        )
        if prior is not None:
            return dict(prior)
        saved: Any = (
            connection.execute(
                text(
                    "SELECT a.*,s.work_order_id FROM task_session_assignments a JOIN task_sessions s "
                    "ON s.project_id=a.project_id AND s.id=a.child_session_id WHERE a.project_id="
                    ":p AND a.client_action_id=:key"
                ),
                {"p": tools.project_id, "key": gap_key},
            )
            .mappings()
            .one_or_none()
        )
        if saved is None:
            instructions = (
                f"{data.objective}\n\nOriginal acceptance: {data.acceptance_check}\n\n"
                f"Maintenance target approval: {data.target_approval_id}\n"
                f"Use onefoundry.build_extension with the approved target and adapter {target.adapter_id}. "
                "Finish that turn; the separate worker dispatches after it settles. "
                "Read saved extension status after the receipt-driven follow-up and report it with "
                "onefoundry.finish_extension_build. Independent QA and activation remain separate. "
                "Reproduce the acceptance failure before coding; "
                "produce exact candidate evidence for independent QA."
            )
            saved = tools.assignments.delegate(
                tools.project_id,
                tools.session_id,
                tools.attempt_id,
                DelegateRequest(
                    client_action_id=gap_key,
                    profile_id=engineer,
                    objective=instructions,
                    expected_result="Exact maintenance candidate, observed tests, source pin and reconciled"
                    " Execu"
                    "tion Gateway receipt.",
                    acceptance_check=data.acceptance_check,
                    acceptance_kind="operator_review",
                ),
                connection=connection,
            )
        value = {
            "tool_call_id": str(action_id),
            "extension_key": str(gap_key),
            "original_session_id": str(tools.session_id),
            "original_work_order_id": str(scope["work_order_id"]),
            "assignment_id": str(saved["id"]),
            "child_session_id": str(saved["child_session_id"]),
            "target_approval_id": str(data.target_approval_id),
            "operation": data.operation,
            "gap_source_id": str(data.gap_source_id) if data.gap_source_id else None,
            "gap_revision": data.gap_revision,
            "checkpoint": data.checkpoint,
            "acceptance_check": data.acceptance_check,
            "status": "assigned",
        }
        gateway._event(
            connection, tools.project_id, scope["work_order_id"], None, "self_extension_requested", value
        )
        return value


def build(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, approval_id: UUID, adapter_id: str
) -> dict[str, Any]:
    from onefoundry.repository_code_contract import RepositoryCodeLaunch

    with tools.gateway.engine.connect() as connection:
        linked = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND event_type='self_extension_requested' "
                "AND payload->>'child_session_id'=:s AND payload->>'target_approval_id'=:approval LIMIT 1"
            ),
            {"p": tools.project_id, "s": str(tools.session_id), "approval": str(approval_id)},
        )
    if linked is None:
        raise Conflict("Engineer coding must be linked to an approved extension assignment")
    from onefoundry.profile_hierarchy import lock_project
    from onefoundry.repository_code_gateway import capability

    execution = tools.gateway._execution_gateway()
    if not execution.enabled:
        raise Unavailable("external coding is disabled")
    capability(execution, adapter_id)
    data = RepositoryCodeLaunch.model_validate(
        {"target_approval_id": approval_id, "idempotency_key": action_id, "adapter_id": adapter_id}
    )
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        current_source(
            connection, tools.project_id, approved_target(connection, tools.project_id, approval_id)
        )
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='self_extension_build_requested' "
                "AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is not None:
            return dict(prior)
        value = {
            "tool_call_id": str(action_id),
            "session_id": str(tools.session_id),
            "work_order_id": str(scope["work_order_id"]),
            "source_attempt_id": str(tools.attempt_id),
            "launch": data.model_dump(mode="json"),
            "status": "waiting_for_turn",
            "next_step": "Finish this turn. The worker dispatches after the active model run settles.",
        }
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "self_extension_build_requested",
            value,
        )
        return value
