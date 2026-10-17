"""Operator HTTP routes for coordination rules and signals; reads never create work."""

from uuid import UUID

from fastapi import APIRouter, Query

from onefoundry.coordination_schemas import (
    AgentRuleAccessChange,
    PauseChange,
    RuleCreate,
    RuleEnabled,
    RuleOut,
    RuleRevise,
    SignalOut,
    StatusOut,
    ToolCeiling,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_coordination_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/coordination")

    @router.get("", response_model=StatusOut)
    def coordination_status(project_id: UUID) -> StatusOut:
        return execute(lambda: StatusOut.model_validate(gateway.coordination_status(project_id)))

    @router.post("/pause", response_model=StatusOut)
    def pause(project_id: UUID, data: PauseChange) -> StatusOut:
        return execute(
            lambda: StatusOut.model_validate(
                gateway.set_coordination_paused(project_id, True, data.expected_version)
            )
        )

    @router.post("/resume", response_model=StatusOut)
    def resume(project_id: UUID, data: PauseChange) -> StatusOut:
        return execute(
            lambda: StatusOut.model_validate(
                gateway.set_coordination_paused(project_id, False, data.expected_version)
            )
        )

    @router.put("/agent-rule-access", response_model=StatusOut)
    def agent_rule_access(project_id: UUID, data: AgentRuleAccessChange) -> StatusOut:
        return execute(
            lambda: StatusOut.model_validate(
                gateway.set_agent_rule_access(project_id, data.enabled, data.expected_version)
            )
        )

    @router.put("/tool-ceiling", response_model=StatusOut)
    def set_tool_ceiling(project_id: UUID, data: ToolCeiling) -> StatusOut:
        return execute(
            lambda: StatusOut.model_validate(
                gateway.set_profile_tool_ceiling(project_id, data.tools, data.expected_version)
            )
        )

    @router.get("/rules", response_model=list[RuleOut])
    def list_rules(project_id: UUID) -> list[RuleOut]:
        return execute(
            lambda: [RuleOut.model_validate(x) for x in gateway.list_coordination_rules(project_id)]
        )

    @router.post("/rules", response_model=RuleOut, status_code=201)
    def create_rule(project_id: UUID, data: RuleCreate) -> RuleOut:
        return execute(lambda: RuleOut.model_validate(gateway.create_coordination_rule(project_id, data)))

    @router.put("/rules/{rule_id}", response_model=RuleOut)
    def revise_rule(project_id: UUID, rule_id: UUID, data: RuleRevise) -> RuleOut:
        return execute(
            lambda: RuleOut.model_validate(gateway.revise_coordination_rule(project_id, rule_id, data))
        )

    @router.post("/rules/{rule_id}/enabled", response_model=RuleOut)
    def set_enabled(project_id: UUID, rule_id: UUID, data: RuleEnabled) -> RuleOut:
        return execute(
            lambda: RuleOut.model_validate(gateway.set_coordination_rule_enabled(project_id, rule_id, data))
        )

    @router.get("/signals", response_model=list[SignalOut])
    def list_signals(project_id: UUID, limit: int = Query(default=50, ge=1, le=200)) -> list[SignalOut]:
        return execute(
            lambda: [
                SignalOut.model_validate(x) for x in gateway.list_coordination_signals(project_id, limit)
            ]
        )

    return router
