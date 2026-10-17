"""Admit source-grounded candidate claims from confirmed linked run output."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError, field_validator
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.onefoundry_organization_store import organization, source
from onefoundry.workflow_schemas import StrictModel, local_text


class CandidateItem(StrictModel):
    source_id: UUID
    claim_text: str = Field(min_length=1, max_length=2000)
    evidence_locator: str = Field(min_length=1, max_length=500)
    observed_at: datetime

    @field_validator("claim_text", "evidence_locator")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)


_ITEMS = TypeAdapter(list[CandidateItem])


def from_run(gateway: Any, project_id: UUID, organization_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.begin() as connection:
        organization(connection, project_id, organization_id)
        run = (
            connection.execute(
                text(
                    "SELECT r.work_order_id,r.status,r.mode,l.step_key FROM runs r "
                    "JOIN onefoundry_work_order_subjects l ON l.project_id=r.project_id "
                    "AND l.work_order_id=r.work_order_id WHERE r.project_id=:p AND r.id=:run "
                    "AND l.organization_id=:o FOR UPDATE OF r"
                ),
                {"p": project_id, "run": run_id, "o": organization_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise NotFound("linked research run not found")
        if run["step_key"] != "research" or run["status"] != "done":
            raise Conflict("research run does not have a confirmed successful output")
        if run["mode"] == "live_text":
            body = connection.scalar(
                text("SELECT text FROM live_text_outputs WHERE project_id=:p AND run_id=:run"),
                {"p": project_id, "run": run_id},
            )
        elif run["mode"] == "synthetic_text":
            body = connection.scalar(
                text(
                    "SELECT text FROM text_outputs WHERE project_id=:p AND run_id=:run AND role='Researcher'"
                ),
                {"p": project_id, "run": run_id},
            )
        else:
            body = None
        if body is None:
            raise Conflict("confirmed research output is unavailable")
        try:
            parsed = json.loads(body)
            items = _ITEMS.validate_python(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise Conflict("research output is not a valid candidate array") from exc
        if not items or len(items) > 20:
            raise Conflict("research output must contain 1 to 20 candidates")
        validated: list[tuple[CandidateItem, dict[str, Any]]] = []
        for item in items:
            src = source(connection, project_id, item.source_id)
            if src["organization_id"] != organization_id:
                raise NotFound("source not found in district")
            if src["superseded_by"] or src["observed_at"] != item.observed_at:
                raise Conflict("source changed since research")
            if src["source_kind"] == "public_excerpt":
                supported = item.claim_text.casefold() in (src["excerpt"] or "").casefold()
            else:
                chunks = connection.execute(
                    text(
                        "SELECT content,source_label FROM input_file_chunks "
                        "WHERE project_id=:p AND file_id=:file"
                    ),
                    {"p": project_id, "file": src["input_file_id"]},
                ).mappings()
                supported = any(
                    item.claim_text.casefold() in chunk["content"].casefold()
                    and item.evidence_locator == chunk["source_label"]
                    for chunk in chunks
                )
            if not supported:
                raise Conflict("candidate is not supported by the observed source")
            validated.append((item, src))
        already = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND run_id=:run "
                "AND event_type='onefoundry_research_candidates_imported' LIMIT 1"
            ),
            {"p": project_id, "run": run_id},
        )
        if already is not None:
            raise Conflict("candidates already imported from this run")
        saved: list[dict[str, Any]] = []
        for item, src in validated:
            claim_id = uuid4()
            row = (
                connection.execute(
                    text(
                        "INSERT INTO onefoundry_claims "
                        "(id,project_id,actor_id,organization_id,source_id,source_revision,"
                        "claim_text,locator,observed_at) "
                        "VALUES (:id,:p,:actor,:o,:source,:revision,:claim,:locator,:observed) "
                        "RETURNING id,source_id,claim_text,locator,review_state,revision"
                    ),
                    {
                        "id": claim_id,
                        "p": project_id,
                        "actor": gateway.actor_id,
                        "o": organization_id,
                        "source": item.source_id,
                        "revision": src["revision"],
                        "claim": item.claim_text,
                        "locator": item.evidence_locator,
                        "observed": item.observed_at,
                    },
                )
                .mappings()
                .one()
            )
            saved.append(dict(row))
        connection.execute(
            text(
                "UPDATE onefoundry_workflows SET status='review_needed',revision=revision+1 "
                "WHERE project_id=:p AND organization_id=:o"
            ),
            {"p": project_id, "o": organization_id},
        )
        gateway._event(
            connection,
            project_id,
            run["work_order_id"],
            run_id,
            "onefoundry_research_candidates_imported",
            {"claim_ids": [str(item["id"]) for item in saved]},
        )
        return saved
