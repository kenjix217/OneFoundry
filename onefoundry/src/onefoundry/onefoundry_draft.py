"""Revisioned outreach prose with current, pinned fact references."""

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.onefoundry_organization_store import organization
from onefoundry.workflow_schemas import StrictModel, local_text


class DraftSegment(StrictModel):
    kind: Literal["plain", "district_fact", "company_fact"]
    text: str = Field(min_length=1, max_length=2000)
    claim_id: UUID | None = None
    statement_id: UUID | None = None
    statement_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_shape(self) -> "DraftSegment":
        self.text = local_text(self.text)
        if self.kind == "plain" and (self.claim_id or self.statement_id or self.statement_version):
            raise ValueError("plain segment cannot cite a fact")
        if self.kind == "district_fact" and (
            not self.claim_id or self.statement_id or self.statement_version
        ):
            raise ValueError("district fact requires exactly one claim")
        if self.kind == "company_fact" and (
            not self.statement_id or not self.statement_version or self.claim_id
        ):
            raise ValueError("company fact requires a statement ID and version")
        return self


class DraftCreate(StrictModel):
    context_packet_id: UUID
    segments: list[DraftSegment] = Field(min_length=1, max_length=20)
    expected_revision: int = Field(ge=0)


def list_drafts(connection: Any, project_id: UUID, organization_id: UUID) -> list[dict[str, Any]]:
    organization(connection, project_id, organization_id)
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT id,project_id,organization_id,context_packet_id,revision,segments,created_at "
                "FROM onefoundry_drafts WHERE project_id=:p AND organization_id=:o ORDER BY revision DESC"
            ),
            {"p": project_id, "o": organization_id},
        ).mappings()
    ]


def save_draft(gateway: Any, project_id: UUID, organization_id: UUID, data: DraftCreate) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        org = organization(connection, project_id, organization_id)
        if org["qualification"] != "qualified":
            raise Conflict("district must be qualified before saving outreach")
        workflow = (
            connection.execute(
                text(
                    "SELECT id,status FROM onefoundry_workflows "
                    "WHERE project_id=:p AND organization_id=:o FOR UPDATE"
                ),
                {"p": project_id, "o": organization_id},
            )
            .mappings()
            .one()
        )
        packet = (
            connection.execute(
                text(
                    "SELECT id,claim_ids,statement_versions,work_order_id FROM onefoundry_context_packets "
                    "WHERE project_id=:p AND id=:id AND organization_id=:o"
                ),
                {"p": project_id, "o": organization_id, "id": data.context_packet_id},
            )
            .mappings()
            .one_or_none()
        )
        if packet is None:
            raise NotFound("context packet not found in district")
        draft_link = connection.scalar(
            text(
                "SELECT 1 FROM onefoundry_work_order_subjects WHERE project_id=:p AND organization_id=:o "
                "AND work_order_id=:order AND step_key='draft'"
            ),
            {"p": project_id, "o": organization_id, "order": packet["work_order_id"]},
        )
        if draft_link is None:
            raise Conflict("context packet is not linked to a draft order")
        latest = connection.scalar(
            text(
                "SELECT coalesce(max(revision),0) FROM onefoundry_drafts "
                "WHERE project_id=:p AND organization_id=:o"
            ),
            {"p": project_id, "o": organization_id},
        )
        if latest != data.expected_revision:
            raise Conflict("draft revision changed; reload before editing")
        pinned_claims = set(packet["claim_ids"])
        pinned_statements = {(item["id"], item["version"]) for item in packet["statement_versions"]}
        for segment in data.segments:
            if segment.kind == "district_fact":
                if str(segment.claim_id) not in pinned_claims:
                    raise Conflict("claim is absent from pinned context")
                current = connection.scalar(
                    text(
                        "SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
                        "ON s.project_id=c.project_id AND s.id=c.source_id WHERE c.project_id=:p "
                        "AND c.organization_id=:o AND c.id=:id AND c.review_state='approved' "
                        "AND c.superseded_by IS NULL AND s.superseded_by IS NULL "
                        "AND c.source_revision=s.revision"
                    ),
                    {"p": project_id, "o": organization_id, "id": segment.claim_id},
                )
                if current is None:
                    raise Conflict("claim is no longer current and approved")
            elif segment.kind == "company_fact":
                if (str(segment.statement_id), segment.statement_version) not in pinned_statements:
                    raise Conflict("statement version is absent from pinned context")
                current = connection.scalar(
                    text(
                        "SELECT 1 FROM onefoundry_company_statements s WHERE s.project_id=:p "
                        "AND s.id=:id AND s.version=:version AND s.state='approved' "
                        "AND NOT EXISTS (SELECT 1 FROM onefoundry_company_statements newer "
                        "WHERE newer.project_id=s.project_id AND newer.statement_key=s.statement_key "
                        "AND newer.version>s.version)"
                    ),
                    {"p": project_id, "id": segment.statement_id, "version": segment.statement_version},
                )
                if current is None:
                    raise Conflict("statement is no longer current and approved")
        draft_id = uuid4()
        row = (
            connection.execute(
                text(
                    "INSERT INTO onefoundry_drafts "
                    "(id,project_id,actor_id,organization_id,context_packet_id,revision,segments) "
                    "VALUES (:id,:p,:actor,:o,:packet,:revision,:segments) "
                    "RETURNING id,project_id,organization_id,context_packet_id,revision,segments,created_at"
                ).bindparams(bindparam("segments", type_=JSONB)),
                {
                    "id": draft_id,
                    "p": project_id,
                    "actor": gateway.actor_id,
                    "o": organization_id,
                    "packet": data.context_packet_id,
                    "revision": latest + 1,
                    "segments": [segment.model_dump(mode="json") for segment in data.segments],
                },
            )
            .mappings()
            .one()
        )
        connection.execute(
            text(
                "UPDATE onefoundry_workflows SET status='draft_ready',revision=revision+1 "
                "WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": workflow["id"]},
        )
        gateway._event(
            connection,
            project_id,
            packet["work_order_id"],
            None,
            "onefoundry_draft_saved",
            {"draft_id": str(draft_id), "context_packet_id": str(packet["id"]), "revision": latest + 1},
        )
        return dict(row)
