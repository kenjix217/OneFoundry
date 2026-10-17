"""Project-scoped organization and source persistence."""

from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.onefoundry_organization_schemas import OrganizationCreate, QualificationChange, SourceCreate


def _one(connection: Any, query: str, values: dict[str, Any], missing: str) -> dict[str, Any]:
    row = connection.execute(text(query), values).mappings().one_or_none()
    if row is None:
        raise NotFound(missing)
    return dict(row)


def organization(connection: Any, project_id: UUID, organization_id: UUID) -> dict[str, Any]:
    return _one(
        connection,
        "SELECT id,project_id,display_name,organization_type,canonical_domain,identity_reviewed,"
        "qualification,revision,created_at FROM onefoundry_organizations "
        "WHERE project_id=:project_id AND id=:id",
        {"project_id": project_id, "id": organization_id},
        "organization not found",
    )


def list_organizations(connection: Any, project_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT id,project_id,display_name,organization_type,canonical_domain,identity_reviewed,"
                "qualification,revision,created_at FROM onefoundry_organizations "
                "WHERE project_id=:project_id ORDER BY created_at DESC,id DESC"
            ),
            {"project_id": project_id},
        ).mappings()
    ]


def create_organization(gateway: Any, project_id: UUID, data: OrganizationCreate) -> dict[str, Any]:
    organization_id, workflow_id = uuid4(), uuid4()
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        if data.identity_reviewed and connection.scalar(
            text(
                "SELECT 1 FROM onefoundry_organizations WHERE project_id=:project_id "
                "AND canonical_domain=:domain AND identity_reviewed"
            ),
            {"project_id": project_id, "domain": data.canonical_domain},
        ):
            raise Conflict("reviewed canonical identity already exists")
        try:
            connection.execute(
                text(
                    "INSERT INTO onefoundry_organizations "
                    "(id,project_id,actor_id,display_name,organization_type,"
                    "canonical_domain,identity_reviewed) VALUES "
                    "(:id,:project_id,:actor_id,:display_name,"
                    "'school_district',:domain,:reviewed)"
                ),
                {
                    "id": organization_id,
                    "project_id": project_id,
                    "actor_id": gateway.actor_id,
                    "display_name": data.display_name,
                    "domain": data.canonical_domain,
                    "reviewed": data.identity_reviewed,
                },
            )
        except IntegrityError as exc:
            if (
                getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                == "onefoundry_org_canonical_unique"
            ):
                raise Conflict("reviewed canonical identity already exists") from exc
            raise
        connection.execute(
            text(
                "INSERT INTO onefoundry_workflows (id,project_id,actor_id,organization_id) "
                "VALUES (:id,:project_id,:actor_id,:organization_id)"
            ),
            {
                "id": workflow_id,
                "project_id": project_id,
                "actor_id": gateway.actor_id,
                "organization_id": organization_id,
            },
        )
        if gateway.live_text_settings and gateway.live_text_settings.onefoundry_business_mode in {
            "manual",
            "event",
            "inbox",
        }:
            from onefoundry.onefoundry_budget import snapshot

            snapshot(connection, gateway, project_id, workflow_id)
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "onefoundry_organization_created",
            {"organization_id": str(organization_id)},
        )
        return organization(connection, project_id, organization_id)


def source(connection: Any, project_id: UUID, source_id: UUID) -> dict[str, Any]:
    return _one(
        connection,
        "SELECT id,project_id,organization_id,source_kind,official_url,excerpt,input_file_id,digest,"
        "observed_at,revision,superseded_by,created_at FROM onefoundry_sources "
        "WHERE project_id=:project_id AND id=:id",
        {"project_id": project_id, "id": source_id},
        "source not found",
    )


def list_sources(connection: Any, project_id: UUID, organization_id: UUID) -> list[dict[str, Any]]:
    organization(connection, project_id, organization_id)
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT id,project_id,organization_id,source_kind,official_url,excerpt,input_file_id,digest,"
                "observed_at,revision,superseded_by,created_at FROM onefoundry_sources "
                "WHERE project_id=:project_id AND organization_id=:organization_id "
                "ORDER BY created_at DESC,id DESC"
            ),
            {"project_id": project_id, "organization_id": organization_id},
        ).mappings()
    ]


def create_source(
    gateway: Any, project_id: UUID, organization_id: UUID, data: SourceCreate
) -> dict[str, Any]:
    source_id = uuid4()
    with gateway.engine.begin() as connection:
        org = organization(connection, project_id, organization_id)
        workflow = (
            connection.execute(
                text(
                    "SELECT revision,status FROM onefoundry_workflows "
                    "WHERE project_id=:project_id AND organization_id=:organization_id FOR UPDATE"
                ),
                {"project_id": project_id, "organization_id": organization_id},
            )
            .mappings()
            .one()
        )
        if workflow["revision"] != data.expected_workflow_revision:
            raise Conflict("workflow revision changed; reload before adding source")
        if workflow["status"] == "unknown":
            raise Conflict("reconcile unknown workflow outcome before changing evidence")
        if data.source_kind == "input_file":
            file = (
                connection.execute(
                    text("SELECT sha256,status FROM input_files WHERE project_id=:project_id AND id=:id"),
                    {"project_id": project_id, "id": data.input_file_id},
                )
                .mappings()
                .one_or_none()
            )
            if file is None:
                raise NotFound("input file not found")
            if file["status"] not in ("ready_text", "ready_visual"):
                raise Conflict("input file is not verified ready")
            digest = file["sha256"]
        else:
            from urllib.parse import urlsplit

            if (
                org["identity_reviewed"]
                and urlsplit(data.official_url or "").hostname != org["canonical_domain"]
            ):
                raise Conflict("source URL does not match reviewed canonical domain")
            digest = sha256((data.excerpt or "").encode()).hexdigest()
        if data.supersedes_source_id:
            previous = source(connection, project_id, data.supersedes_source_id)
            if previous["organization_id"] != organization_id:
                raise NotFound("source not found")
            if previous["superseded_by"] is not None:
                raise Conflict("source is already superseded")
        connection.execute(
            text(
                "INSERT INTO onefoundry_sources "
                "(id,project_id,actor_id,organization_id,source_kind,official_url,"
                "excerpt,input_file_id,digest,observed_at) VALUES "
                "(:id,:project_id,:actor_id,:organization_id,"
                ":source_kind,:official_url,:excerpt,:input_file_id,:digest,:observed_at)"
            ),
            {
                "id": source_id,
                "project_id": project_id,
                "actor_id": gateway.actor_id,
                "organization_id": organization_id,
                "source_kind": data.source_kind,
                "official_url": data.official_url,
                "excerpt": data.excerpt,
                "input_file_id": data.input_file_id,
                "digest": digest,
                "observed_at": data.observed_at,
            },
        )
        if data.supersedes_source_id:
            connection.execute(
                text(
                    "UPDATE onefoundry_sources SET superseded_by=:new_id,revision=revision+1 "
                    "WHERE project_id=:project_id AND id=:old_id AND superseded_by IS NULL"
                ),
                {"new_id": source_id, "project_id": project_id, "old_id": data.supersedes_source_id},
            )
        changed = connection.execute(
            text(
                "UPDATE onefoundry_workflows SET status='review_needed',"
                "blocked_reason=NULL,revision=revision+1 "
                "WHERE project_id=:project_id AND organization_id=:organization_id AND revision=:revision"
            ),
            {
                "project_id": project_id,
                "organization_id": organization_id,
                "revision": data.expected_workflow_revision,
            },
        )
        if changed.rowcount != 1:
            raise Conflict("workflow revision changed; reload before adding source")
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "onefoundry_source_added",
            {"organization_id": str(organization_id), "source_id": str(source_id), "digest": digest},
        )
        return source(connection, project_id, source_id)


def qualify(
    gateway: Any, project_id: UUID, organization_id: UUID, data: QualificationChange
) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        org = organization(connection, project_id, organization_id)
        if org["revision"] != data.expected_revision:
            raise Conflict("organization revision changed; reload before deciding")
        if data.qualification == "qualified":
            approved = connection.scalar(
                text(
                    "SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
                    "ON s.project_id=c.project_id AND s.id=c.source_id "
                    "WHERE c.project_id=:project_id AND c.organization_id=:organization_id "
                    "AND c.review_state='approved' AND c.superseded_by IS NULL AND s.superseded_by IS NULL "
                    "AND c.source_revision=s.revision LIMIT 1"
                ),
                {"project_id": project_id, "organization_id": organization_id},
            )
            if approved is None:
                raise Conflict("qualification requires a current approved claim")
        changed = connection.execute(
            text(
                "UPDATE onefoundry_organizations SET qualification=:qualification,revision=revision+1 "
                "WHERE project_id=:project_id AND id=:id AND revision=:revision"
            ),
            {
                "qualification": data.qualification,
                "project_id": project_id,
                "id": organization_id,
                "revision": data.expected_revision,
            },
        )
        if changed.rowcount != 1:
            raise Conflict("organization revision changed; reload before deciding")
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "onefoundry_qualification_changed",
            {"organization_id": str(organization_id), "qualification": data.qualification},
        )
        if (
            data.qualification == "qualified"
            and gateway.live_text_settings is not None
            and gateway.live_text_settings.onefoundry_business_mode in {"event", "inbox"}
        ):
            from onefoundry.onefoundry_intent import enqueue_draft_intent

            enqueue_draft_intent(connection, gateway, project_id, organization_id)
        return organization(connection, project_id, organization_id)
