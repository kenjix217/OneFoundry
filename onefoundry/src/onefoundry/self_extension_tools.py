"""Native team-tool admission, advertisements and receipts for extension work."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import text

from onefoundry.capability_setup_contract import CapabilitySetup
from onefoundry.company_knowledge_proposal import KnowledgeDraft
from onefoundry.extension_activation_contract import ExtensionRelease
from onefoundry.extension_release_preparation_contract import PrepareRelease
from onefoundry.repository_code_contract import RepositoryCodeTarget
from onefoundry.requested_capability import InspectCapability
from onefoundry.self_extension_request import ExtensionRequest, build, request
from onefoundry.self_extension_results import finish_build, finish_qa, launch_qa
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from sqlalchemy import Connection

    from onefoundry.task_session_tools import TaskSessionTools

READ = "onefoundry.read_extension_status"
INSPECT = "onefoundry.inspect_capability"
REQUEST = "onefoundry.request_extension"
BUILD = "onefoundry.build_extension"
FINISH_BUILD = "onefoundry.finish_extension_build"
VERIFY = "onefoundry.verify_extension_candidate"
FINISH_QA = "onefoundry.finish_extension_review"
ACTIVATE = "onefoundry.propose_extension_activation"
KNOWLEDGE = "onefoundry.propose_company_knowledge"
TARGET_PROPOSAL = "onefoundry.propose_maintenance_target"
SETUP = "onefoundry.propose_capability_setup"
PREPARE = "onefoundry.prepare_extension_release"
TOOLS = frozenset(
    (
        READ,
        INSPECT,
        REQUEST,
        BUILD,
        FINISH_BUILD,
        VERIFY,
        FINISH_QA,
        ACTIVATE,
        KNOWLEDGE,
        TARGET_PROPOSAL,
        SETUP,
        PREPARE,
    )
)


class BuildArgs(StrictModel):
    target_approval_id: UUID
    adapter_id: Literal["codex_cli", "claude_code"]


class RunArgs(StrictModel):
    run_id: UUID


class QaArgs(StrictModel):
    qa_job_id: UUID


def admitted(scope: Mapping[str, Any]) -> set[str]:
    chief = scope["kind"] == "main" and scope["is_project_chief"]
    engineer = scope.get("profile_kind") == "builtin" and scope.get("profile_name") == "Engineer"
    qa = scope.get("profile_kind") == "builtin" and scope.get("profile_name") == "QA"
    result = {READ} if chief or engineer or qa else set()
    if chief:
        result.add(INSPECT)
    if not scope.get("actionable", True):
        return result
    if chief:
        result.update((REQUEST, ACTIVATE, KNOWLEDGE, TARGET_PROPOSAL, SETUP, PREPARE))
    if engineer:
        result.update((BUILD, FINISH_BUILD))
    if qa and scope.get("assignment") and scope["assignment"]["acceptance_kind"] == "qa_review":
        result.update((VERIFY, FINISH_QA))
    return result


def specifications(allowed: frozenset[str]) -> list[dict[str, Any]]:
    descriptions = {
        PREPARE: "After independent QA, ask the separate packaging worker to build the candidate. "
        "Include configuration and migration effects and the rollback plan. This installs nothing. "
        "Finish the turn; packaging completion resumes this task with the exact release record.",
        SETUP: "Prepare an exact approval for existing tools needed by named agents in this project. "
        "Use this for missing permissions instead of handing the operator a setup checklist. "
        "For repository access include the selected source revision and a real verification file. "
        "If it is not mounted, choose repository_alias from the current host_repositories offers "
        "instead of source_id; never invent a host path. "
        "Approval applies the grant, verifies the read and resumes this task once. Finish this turn.",
        TARGET_PROPOSAL: "Propose one exact maintenance scope for this original task in the approval inbox. "
        "Choose a registered local source, explicit edit paths and acceptance commands, a pinned test image "
        "and configured provider endpoint. The server freezes the Engineer model and execution limits. "
        "This grants nothing before the operator approves it.",
        INSPECT: "Inspect canonical registration and the current profile grant for one exact tool. "
        "Existing denied tools require access. Registration is not execution or dependency success.",
        READ: "Read this original task's linked coding runs and QA job identities. A "
        "saved candidate is not installed software.",
        REQUEST: "Commission Engineer for one freshly verified implementation gap and sa"
        "ve the original checkpoint. Requires an operator-approved maintenance "
        "target. Setup, denial and unknown outcomes cannot create extension wor"
        "k.",
        BUILD: "Dispatch this linked Engineer extension to the separate isolated codin"
        "g runner. Requires exact approved scope and fresh isolation/egress pro"
        "of. Finish this turn after dispatch.",
        FINISH_BUILD: "Report this Engineer's exact reconciled candidate and close the assign"
        "ment. Queued or unknown is not completion. Finish the turn afterward.",
        VERIFY: "Queue independent reconstruction and tests for the exact candidate fro"
        "m this QA assignment's confirmed Engineer dependency. Finish the turn "
        "after dispatch.",
        FINISH_QA: "Submit the observed independent QA receipt and exact candidate verdict"
        ". Engineer prose cannot satisfy this check. Finish the turn afterward.",
        ACTIVATE: "Prepare an exact release and independent QA bound operator approval. T"
        "his queues no installation before the operator approves the concrete p"
        "ackage, effects and rollback plan.",
        KNOWLEDGE: "Propose bounded company knowledge with exact committed source citation"
        "s and matching excerpts. Source reads must be admitted. The operator r"
        "eviews capture, then existing memory review owns publication; a privat"
        "e artifact is not a published fact.",
    }
    models: dict[str, type[StrictModel]] = {
        PREPARE: PrepareRelease,
        SETUP: CapabilitySetup,
        TARGET_PROPOSAL: RepositoryCodeTarget,
        REQUEST: ExtensionRequest,
        BUILD: BuildArgs,
        FINISH_BUILD: RunArgs,
        VERIFY: RunArgs,
        FINISH_QA: QaArgs,
        ACTIVATE: ExtensionRelease,
        KNOWLEDGE: KnowledgeDraft,
        INSPECT: InspectCapability,
    }
    return [
        {
            "type": "function",
            "name": name.split(".", 1)[1],
            "description": descriptions[name],
            "inputSchema": models[name].model_json_schema()
            if name in models
            else {"type": "object", "properties": {}, "additionalProperties": False},
        }
        for name in sorted(TOOLS & allowed)
    ]


def execute(
    tools: TaskSessionTools,
    scope: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any],
    action_id: UUID,
) -> dict[str, Any]:
    if name == READ:
        from onefoundry.capability_setup import status as setup_status
        from onefoundry.self_extension_continuation import status
        from onefoundry.self_extension_release import status as release_status

        if arguments:
            raise ValueError("extension status takes no arguments")
        return {
            "extensions": status(tools.gateway, tools.project_id, scope["root_work_order_id"]),
            "setup": setup_status(tools.gateway, tools.project_id, scope["root_work_order_id"]),
            "releases": release_status(tools.gateway, tools.project_id, scope["root_work_order_id"]),
        }
    if name == PREPARE:
        from onefoundry.self_extension_release import request as prepare_release

        return prepare_release(tools, dict(scope), action_id, PrepareRelease.model_validate(arguments))
    if name == SETUP:
        from onefoundry.capability_setup import propose as propose_setup

        return propose_setup(tools, dict(scope), action_id, CapabilitySetup.model_validate(arguments))
    if name == TARGET_PROPOSAL:
        from onefoundry.self_extension_target import propose as propose_target

        return propose_target(tools, dict(scope), action_id, RepositoryCodeTarget.model_validate(arguments))
    if name == INSPECT:
        from onefoundry.requested_capability import inspect

        inspection = InspectCapability.model_validate(arguments)
        return inspect(
            tools.gateway, tools.project_id, inspection.profile_id or scope["profile_id"], inspection.tool_id
        ).model_dump(mode="json")
    if name == REQUEST:
        return request(tools, dict(scope), action_id, ExtensionRequest.model_validate(arguments))
    if name == ACTIVATE:
        from onefoundry.self_extension_activation import propose

        return propose(tools, dict(scope), action_id, ExtensionRelease.model_validate(arguments))
    if name == KNOWLEDGE:
        from onefoundry.company_knowledge_proposal import propose as propose_knowledge

        return propose_knowledge(tools, dict(scope), action_id, KnowledgeDraft.model_validate(arguments))
    if name == BUILD:
        data = BuildArgs.model_validate(arguments)
        return build(tools, dict(scope), action_id, data.target_approval_id, data.adapter_id)
    if name == FINISH_BUILD:
        return finish_build(tools, dict(scope), action_id, RunArgs.model_validate(arguments).run_id)
    if name == VERIFY:
        return launch_qa(tools, dict(scope), action_id, RunArgs.model_validate(arguments).run_id)
    return finish_qa(tools, dict(scope), action_id, QaArgs.model_validate(arguments).qa_job_id)


def readback(
    tools: TaskSessionTools, connection: Connection, action_id: UUID, name: str
) -> dict[str, Any] | None:
    if name in (REQUEST, VERIFY, ACTIVATE, KNOWLEDGE, TARGET_PROPOSAL, SETUP, PREPARE):
        event = {
            PREPARE: "self_extension_release_requested",
            SETUP: "capability_setup_proposed",
            REQUEST: "self_extension_requested",
            ACTIVATE: "self_extension_activation_proposed",
            VERIFY: "self_extension_qa_requested",
            KNOWLEDGE: "company_knowledge_proposed",
            TARGET_PROPOSAL: "self_extension_target_proposed",
        }[name]
        payload = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type=:event "
                "AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "event": event, "id": str(action_id)},
        )
        return dict(payload) if payload is not None else None
    if name == BUILD:
        value = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='self_extension_build_requested' "
                "AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if value is not None:
            return dict(value)
        row = (
            connection.execute(
                text(
                    "SELECT run_id,id,status FROM external_code_attempts WHERE project_id=:p "
                    "AND idempotency_key=:id AND fixture_id='repository_code_v1'"
                ),
                {"p": tools.project_id, "id": action_id},
            )
            .mappings()
            .one_or_none()
        )
        return (
            {"run_id": str(row["run_id"]), "attempt_id": str(row["id"]), "status": row["status"]}
            if row is not None
            else None
        )
    row = (
        connection.execute(
            text(
                "SELECT id,result_message_id FROM task_session_assignments WHERE projec"
                "t_id=:p "
                "AND child_session_id=:s AND result_action_id=:id AND status='satisfied'"
            ),
            {"p": tools.project_id, "s": tools.session_id, "id": action_id},
        )
        .mappings()
        .one_or_none()
    )
    return (
        {"assignment_id": str(row["id"]), "result_message_id": str(row["result_message_id"])}
        if row is not None
        else None
    )
