"""Claim provenance, human review, and freshness readback."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.onefoundry_organization_schemas import ClaimCreate, ClaimReview
from onefoundry.onefoundry_organization_store import organization, source

_READ = (
    "SELECT c.id,c.project_id,c.organization_id,c.source_id,c.source_revision,c.claim_text,c.locator,"
    "c.observed_at,c.review_state,c.verification_state,c.revision,c.superseded_by,c.created_at,"
    "(c.superseded_by IS NULL AND s.superseded_by IS NULL AND c.source_revision=s.revision) AS fresh "
    "FROM onefoundry_claims c JOIN onefoundry_sources s ON s.project_id=c.project_id AND s.id=c.source_id "
)


def claim(connection: Any, project_id: UUID, claim_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text(_READ + "WHERE c.project_id=:project_id AND c.id=:id"),
            {"project_id": project_id, "id": claim_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("claim not found")
    return dict(row)


def list_claims(connection: Any, project_id: UUID, organization_id: UUID) -> list[dict[str, Any]]:
    organization(connection, project_id, organization_id)
    return [
        dict(row)
        for row in connection.execute(
            text(
                _READ + "WHERE c.project_id=:project_id AND c.organization_id=:organization_id "
                "ORDER BY c.created_at DESC,c.id DESC"
            ),
            {"project_id": project_id, "organization_id": organization_id},
        ).mappings()
    ]


def create_claim(gateway: Any, project_id: UUID, organization_id: UUID, data: ClaimCreate) -> dict[str, Any]:
    claim_id = uuid4()
    with gateway.engine.begin() as connection:
        src = source(connection, project_id, data.source_id)
        if src["organization_id"] != organization_id:
            raise NotFound("source not found")
        if src["superseded_by"] is not None:
            raise Conflict("source was superseded")
        if src["revision"] != data.expected_source_revision:
            raise Conflict("source revision changed; reload before proposing claim")
        if src["source_kind"] == "public_excerpt":
            if data.claim_text.casefold() not in (src["excerpt"] or "").casefold():
                raise Conflict("claim must quote the observed excerpt")
        else:
            chunks = connection.execute(
                text(
                    "SELECT content,source_label FROM input_file_chunks "
                    "WHERE project_id=:project_id AND file_id=:file_id"
                ),
                {"project_id": project_id, "file_id": src["input_file_id"]},
            ).mappings()
            if not any(
                data.claim_text.casefold() in row["content"].casefold()
                and data.locator == row["source_label"]
                for row in chunks
            ):
                raise Conflict("claim requires a matching extracted input-file chunk and locator")
        if data.supersedes_claim_id:
            old = claim(connection, project_id, data.supersedes_claim_id)
            if old["organization_id"] != organization_id or old["superseded_by"] is not None:
                raise Conflict("claim cannot be superseded")
        connection.execute(
            text(
                "INSERT INTO onefoundry_claims "
                "(id,project_id,actor_id,organization_id,source_id,source_revision,"
                "claim_text,locator,observed_at) VALUES "
                "(:id,:project_id,:actor_id,:organization_id,:source_id,"
                ":source_revision,:claim_text,:locator,:observed_at)"
            ),
            {
                "id": claim_id,
                "project_id": project_id,
                "actor_id": gateway.actor_id,
                "organization_id": organization_id,
                "source_id": data.source_id,
                "source_revision": src["revision"],
                "claim_text": data.claim_text,
                "locator": data.locator,
                "observed_at": src["observed_at"],
            },
        )
        if data.supersedes_claim_id:
            changed = connection.execute(
                text(
                    "UPDATE onefoundry_claims SET superseded_by=:new_id,revision=revision+1 "
                    "WHERE project_id=:project_id AND id=:old_id AND superseded_by IS NULL"
                ),
                {"new_id": claim_id, "project_id": project_id, "old_id": data.supersedes_claim_id},
            )
            if changed.rowcount != 1:
                raise Conflict("claim was concurrently superseded")
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "onefoundry_claim_proposed",
            {
                "organization_id": str(organization_id),
                "claim_id": str(claim_id),
                "source_id": str(data.source_id),
            },
        )
        return claim(connection, project_id, claim_id)


def review_claim(gateway: Any, project_id: UUID, claim_id: UUID, data: ClaimReview) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        current = claim(connection, project_id, claim_id)
        if current["revision"] != data.expected_revision or current["review_state"] != "candidate":
            raise Conflict("claim review state changed; reload before deciding")
        if not current["fresh"]:
            raise Conflict("claim source is stale or superseded")
        changed = connection.execute(
            text(
                "UPDATE onefoundry_claims SET review_state=:decision,revision=revision+1 "
                "WHERE project_id=:project_id AND id=:id AND revision=:revision AND review_state='candidate' "
                "AND superseded_by IS NULL"
            ),
            {
                "decision": data.decision,
                "project_id": project_id,
                "id": claim_id,
                "revision": data.expected_revision,
            },
        )
        if changed.rowcount != 1:
            raise Conflict("claim review state changed; reload before deciding")
        if data.decision == "approved":
            connection.execute(
                text(
                    "UPDATE onefoundry_workflows SET status='research_reviewed',revision=revision+1 "
                    "WHERE project_id=:project_id AND organization_id=:organization_id "
                    "AND status NOT IN ('unknown','blocked')"
                ),
                {"project_id": project_id, "organization_id": current["organization_id"]},
            )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "onefoundry_claim_reviewed",
            {"claim_id": str(claim_id), "decision": data.decision},
        )
        return claim(connection, project_id, claim_id)
