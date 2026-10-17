"""HTTP readback and governed mutation routes for organization records."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.onefoundry_budget import context_limit
from onefoundry.onefoundry_claim_store import list_claims
from onefoundry.onefoundry_company_statements import (
    StatementCreate,
    StatementDecision,
    list_statements,
)
from onefoundry.onefoundry_draft import DraftCreate, list_drafts
from onefoundry.onefoundry_intent import latest_intent
from onefoundry.onefoundry_next_action import (
    OneFoundryActionSnapshot,
    OneFoundryNextAction,
    next_action,
)
from onefoundry.onefoundry_organization_schemas import (
    ClaimCreate,
    ClaimOut,
    ClaimReview,
    OrganizationCreate,
    OrganizationOut,
    QualificationChange,
    SourceCreate,
    SourceOut,
)
from onefoundry.onefoundry_organization_store import list_organizations, list_sources, organization
from onefoundry.onefoundry_workflow import LinkedOrderRequest
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


class OrganizationDetail(BaseModel):
    organization: OrganizationOut
    sources: list[SourceOut]
    claims: list[ClaimOut]
    workflow_status: str
    workflow_revision: int
    blocked_reason: str | None = None
    next_action: OneFoundryNextAction
    linked_order: dict[str, object] | None = None
    context_packet: dict[str, object] | None = None
    budget: dict[str, object] | None = None
    intent: dict[str, object] | None = None
    drafts: list[dict[str, object]] = []


def make_onefoundry_organization_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/organizations", response_model=list[OrganizationOut])
    def organizations(project_id: UUID) -> list[OrganizationOut]:
        def read() -> list[OrganizationOut]:
            with gateway.engine.connect() as connection:
                gateway._require_project(connection, project_id)
                return [
                    OrganizationOut.model_validate(row) for row in list_organizations(connection, project_id)
                ]

        return execute(read)

    @router.post("/organizations", response_model=OrganizationOut, status_code=201)
    def create(project_id: UUID, data: OrganizationCreate) -> OrganizationOut:
        return execute(
            lambda: OrganizationOut.model_validate(gateway.onefoundry_create_organization(project_id, data))
        )

    @router.get("/organizations/{organization_id}", response_model=OrganizationDetail)
    def detail(project_id: UUID, organization_id: UUID) -> OrganizationDetail:
        def read() -> OrganizationDetail:
            with gateway.engine.connect() as connection:
                org = OrganizationOut.model_validate(organization(connection, project_id, organization_id))
                sources = [
                    SourceOut.model_validate(row)
                    for row in list_sources(connection, project_id, organization_id)
                ]
                claims = [
                    ClaimOut.model_validate(row)
                    for row in list_claims(connection, project_id, organization_id)
                ]
                workflow = (
                    connection.execute(
                        text(
                            "SELECT status,revision,blocked_reason FROM onefoundry_workflows "
                            "WHERE project_id=:project_id "
                            "AND organization_id=:organization_id"
                        ),
                        {"project_id": project_id, "organization_id": organization_id},
                    )
                    .mappings()
                    .one()
                )
                linked: dict[str, object] | None = None
                packet: dict[str, object] | None = None
                budget: dict[str, object] | None = None
                intent: dict[str, object] | None = None
                drafts: list[dict[str, object]] = []
                if gateway.live_text_settings and gateway.live_text_settings.onefoundry_business_mode in {
                    "manual",
                    "event",
                    "inbox",
                }:
                    if gateway.live_text_settings.onefoundry_business_mode in {"event", "inbox"}:
                        intent = latest_intent(connection, project_id, organization_id)
                    linked_row = (
                        connection.execute(
                            text(
                                "SELECT l.work_order_id,l.step_key,o.status,o.profile_id,r.id AS run_id,"
                                "r.status AS run_status FROM onefoundry_work_order_subjects l "
                                "JOIN work_orders o ON o.project_id=l.project_id AND o.id=l.work_order_id "
                                "LEFT JOIN LATERAL (SELECT id,status FROM runs WHERE project_id=l.project_id "
                                "AND work_order_id=l.work_order_id "
                                "ORDER BY started_at DESC,id DESC LIMIT 1) r ON true "
                                "WHERE l.project_id=:p AND l.organization_id=:o "
                                "ORDER BY l.created_at DESC,l.id DESC LIMIT 1"
                            ),
                            {"p": project_id, "o": organization_id},
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if linked_row is not None:
                        linked = dict(linked_row)
                        packet_row = (
                            connection.execute(
                                text(
                                    "SELECT id,work_order_id,claim_ids,statement_versions,excluded,sha256 "
                                    "FROM onefoundry_context_packets "
                                    "WHERE project_id=:p AND work_order_id=:order"
                                ),
                                {"p": project_id, "order": linked_row["work_order_id"]},
                            )
                            .mappings()
                            .one_or_none()
                        )
                        packet = dict(packet_row) if packet_row is not None else None
                    policy = (
                        connection.execute(
                            text(
                                "SELECT id,linked_orders_limit,model_attempts_limit,public_web_calls_limit,"
                                "context_bytes_limit,output_tokens_limit,elapsed_seconds_limit "
                                "FROM onefoundry_budget_snapshots WHERE project_id=:p AND workflow_id=("
                                "SELECT id FROM onefoundry_workflows "
                                "WHERE project_id=:p AND organization_id=:o)"
                            ),
                            {"p": project_id, "o": organization_id},
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if policy is not None:
                        budget = dict(policy)
                        if gateway.live_text_settings is None:
                            raise Conflict("OneFoundry budget settings are unavailable")
                        budget["effective_context_bytes_limit"] = context_limit(
                            gateway.live_text_settings, budget
                        )
                        budget["reservations"] = [
                            dict(row)
                            for row in connection.execute(
                                text(
                                    "SELECT id,kind,state,owner_id,observed_tokens,observed_cost_usd "
                                    "FROM onefoundry_budget_reservations WHERE project_id=:p "
                                    "AND workflow_id=(SELECT id FROM onefoundry_workflows "
                                    "WHERE project_id=:p AND organization_id=:o) ORDER BY created_at,id"
                                ),
                                {"p": project_id, "o": organization_id},
                            ).mappings()
                        ]
                    drafts = list_drafts(connection, project_id, organization_id)
            latest_source = next((src for src in sources if src.superseded_by is None), None)
            snapshot = OneFoundryActionSnapshot(
                mode=gateway.live_text_settings.onefoundry_business_mode
                if gateway.live_text_settings
                else "records",
                workflow_status=workflow["status"],
                workflow_revision=workflow["revision"],
                qualification=org.qualification,
                source_revision=latest_source.revision if latest_source else None,
                has_source=bool(sources),
                has_candidate=any(c.review_state == "candidate" and c.fresh for c in claims),
                has_approved_fresh_claim=any(c.review_state == "approved" and c.fresh for c in claims),
                has_stale_evidence=(any(not c.fresh for c in claims) and not any(c.fresh for c in claims)),
                linked_order_status=str(linked["status"]) if linked else None,
                intent_state=str(intent["state"]) if intent else None,
            )
            return OrganizationDetail(
                organization=org,
                sources=sources,
                claims=claims,
                workflow_status=workflow["status"],
                workflow_revision=workflow["revision"],
                blocked_reason=workflow["blocked_reason"],
                next_action=next_action(snapshot),
                linked_order=linked,
                context_packet=packet,
                budget=budget,
                intent=intent,
                drafts=drafts,
            )

        return execute(read)

    @router.get("/organizations/{organization_id}/sources", response_model=list[SourceOut])
    def sources(project_id: UUID, organization_id: UUID) -> list[SourceOut]:
        def read() -> list[SourceOut]:
            with gateway.engine.connect() as connection:
                return [
                    SourceOut.model_validate(row)
                    for row in list_sources(connection, project_id, organization_id)
                ]

        return execute(read)

    @router.post("/organizations/{organization_id}/sources", response_model=SourceOut, status_code=201)
    def add_source(project_id: UUID, organization_id: UUID, data: SourceCreate) -> SourceOut:
        return execute(
            lambda: SourceOut.model_validate(
                gateway.onefoundry_create_source(project_id, organization_id, data)
            )
        )

    @router.get("/organizations/{organization_id}/claims", response_model=list[ClaimOut])
    def claims(project_id: UUID, organization_id: UUID) -> list[ClaimOut]:
        def read() -> list[ClaimOut]:
            with gateway.engine.connect() as connection:
                return [
                    ClaimOut.model_validate(row)
                    for row in list_claims(connection, project_id, organization_id)
                ]

        return execute(read)

    @router.post("/organizations/{organization_id}/claims", response_model=ClaimOut, status_code=201)
    def add_claim(project_id: UUID, organization_id: UUID, data: ClaimCreate) -> ClaimOut:
        return execute(
            lambda: ClaimOut.model_validate(
                gateway.onefoundry_create_claim(project_id, organization_id, data)
            )
        )

    @router.post("/claims/{claim_id}/review", response_model=ClaimOut)
    def review(project_id: UUID, claim_id: UUID, data: ClaimReview) -> ClaimOut:
        return execute(
            lambda: ClaimOut.model_validate(gateway.onefoundry_review_claim(project_id, claim_id, data))
        )

    @router.post("/organizations/{organization_id}/qualification", response_model=OrganizationOut)
    def qualification(project_id: UUID, organization_id: UUID, data: QualificationChange) -> OrganizationOut:
        return execute(
            lambda: OrganizationOut.model_validate(
                gateway.onefoundry_qualify(project_id, organization_id, data)
            )
        )

    if gateway.live_text_settings and gateway.live_text_settings.onefoundry_business_mode in {
        "manual",
        "event",
        "inbox",
    }:

        @router.get("/company-statements")
        def company_statements(project_id: UUID) -> list[dict[str, object]]:
            def read() -> list[dict[str, object]]:
                with gateway.engine.connect() as connection:
                    gateway._require_project(connection, project_id)
                    return list_statements(connection, project_id)

            return execute(read)

        @router.post("/company-statements", status_code=201)
        def create_statement(project_id: UUID, data: StatementCreate) -> dict[str, object]:
            return execute(lambda: gateway.onefoundry_statement_create(project_id, data))

        @router.post("/company-statements/{statement_id}/approve")
        def approve_statement(
            project_id: UUID, statement_id: UUID, data: StatementDecision
        ) -> dict[str, object]:
            return execute(
                lambda: gateway.onefoundry_statement_decide(project_id, statement_id, "approve", data)
            )

        @router.post("/company-statements/{statement_id}/retire")
        def retire_statement(
            project_id: UUID, statement_id: UUID, data: StatementDecision
        ) -> dict[str, object]:
            return execute(
                lambda: gateway.onefoundry_statement_decide(project_id, statement_id, "retire", data)
            )

        @router.post("/organizations/{organization_id}/research-orders", status_code=201)
        def research_order(
            project_id: UUID, organization_id: UUID, data: LinkedOrderRequest
        ) -> dict[str, object]:
            return execute(
                lambda: gateway.onefoundry_linked_order(project_id, organization_id, "research", data)
            )

        @router.post("/organizations/{organization_id}/draft-orders", status_code=201)
        def draft_order(
            project_id: UUID, organization_id: UUID, data: LinkedOrderRequest
        ) -> dict[str, object]:
            return execute(
                lambda: gateway.onefoundry_linked_order(project_id, organization_id, "draft", data)
            )

        @router.post(
            "/organizations/{organization_id}/research-candidates/from-run/{run_id}", status_code=201
        )
        def import_research_candidates(
            project_id: UUID, organization_id: UUID, run_id: UUID
        ) -> list[dict[str, object]]:
            return execute(
                lambda: gateway.onefoundry_candidates_from_run(project_id, organization_id, run_id)
            )

        @router.get("/organizations/{organization_id}/drafts")
        def drafts(project_id: UUID, organization_id: UUID) -> list[dict[str, object]]:
            def read() -> list[dict[str, object]]:
                with gateway.engine.connect() as connection:
                    return list_drafts(connection, project_id, organization_id)

            return execute(read)

        @router.post("/organizations/{organization_id}/drafts", status_code=201)
        def create_draft(project_id: UUID, organization_id: UUID, data: DraftCreate) -> dict[str, object]:
            return execute(lambda: gateway.onefoundry_save_draft(project_id, organization_id, data))

    return router
