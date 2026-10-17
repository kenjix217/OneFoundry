"""Bounded, metadata-only coding evidence for an admitted role tool.

Saved source revisions describe launch-time bindings, not current repository state.
Test rows are exposed only when the append-only receipt event corroborates them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import Connection, text

from onefoundry.workflow_schemas import RunStatus, StrictModel

if TYPE_CHECKING:
    from onefoundry.role_tool_admission import RunContext

CODING_EVIDENCE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"run_id": {"type": "string", "format": "uuid"}},
    "required": ["run_id"],
    "additionalProperties": False,
}


class CodingEvidenceArguments(StrictModel):
    run_id: UUID


class CodingTest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    status: Literal["passed", "failed"]
    exit_code: int


class CodingArtifact(StrictModel):
    artifact_id: UUID
    kind: Literal["diff", "log"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0, le=65536)


class CodingAttempt(StrictModel):
    attempt_id: UUID
    adapter_id: Literal["codex_cli", "claude_code", "offline_probe"]
    target_id: Literal[
        "engineer_qa_v1", "local_source_probe_v1", "github_snapshot_codex_fixture_v1", "repository_code_v1"
    ]
    status: Literal["queued", "done", "failed", "unknown"]
    reason_code: str | None
    source_kind: Literal["local", "synthetic_github_snapshot", "fixture"]
    source_id: UUID | None
    source_branch: str | None
    source_revision: str | None
    source_tree: str | None
    baseline_commit: str | None
    candidate_commit: str | None
    diff_sha256: str | None
    receipt_sha256: str | None
    receipt_event_verified: bool
    verified_outcome: Literal["done", "failed"] | None
    tests_source: Literal["reconciled_runner_receipt", "none"]
    independent_qa_verified: Literal[False]
    live_isolation_proven: Literal[False]
    tests: list[CodingTest] = Field(max_length=16)
    artifacts: list[CodingArtifact] = Field(max_length=2)

    @model_validator(mode="after")
    def receipt_provenance(self) -> CodingAttempt:
        if self.target_id != "repository_code_v1" and len(self.tests) > 8:
            raise ValueError("legacy coding evidence exceeds its test bound")
        if self.tests_source == "none" and self.tests:
            raise ValueError("test rows require a corroborated receipt")
        if self.tests_source == "reconciled_runner_receipt" and not self.receipt_event_verified:
            raise ValueError("reconciled tests require a receipt digest")
        if not self.receipt_event_verified and (
            self.baseline_commit is not None
            or self.candidate_commit is not None
            or self.diff_sha256 is not None
            or self.receipt_sha256 is not None
            or self.artifacts
        ):
            raise ValueError("receipt fields require a corroborating event")
        if self.receipt_event_verified and self.receipt_sha256 is None:
            raise ValueError("corroborated receipt requires its digest")
        if self.verified_outcome is not None and (
            not self.receipt_event_verified or self.verified_outcome != self.status
        ):
            raise ValueError("verified outcome requires a matching terminal owner record")
        return self


class CodingEvidence(StrictModel):
    run_id: UUID
    found: bool
    run_status: RunStatus | None
    attempts: list[CodingAttempt] = Field(max_length=3)
    has_more: bool

    @model_validator(mode="after")
    def absent_run_has_no_evidence(self) -> CodingEvidence:
        if not self.found and (self.run_status is not None or self.attempts or self.has_more):
            raise ValueError("absent run cannot carry evidence")
        if self.found and self.run_status is None:
            raise ValueError("found run requires status")
        return self


def parse_coding_evidence(arguments: object) -> dict[str, Any]:
    return CodingEvidenceArguments.model_validate(arguments).model_dump(mode="json")


def validate_coding_evidence(result: dict[str, Any]) -> None:
    CodingEvidence.model_validate(result)


def _receipt_tests(
    connection: Connection, project_id: UUID, run_id: UUID, attempt: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    digest = attempt["receipt_sha256"]
    if digest is None:
        return "none", []
    events = connection.execute(
        text(
            "SELECT payload FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='external_code_receipt' LIMIT 16"
        ),
        {"project_id": str(project_id), "run_id": str(run_id)},
    ).mappings()
    for row in events:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("attempt_id") == str(attempt["id"])
            and payload.get("adapter_id") == attempt["adapter_id"]
            and payload.get("receipt_sha256") == digest
            and payload.get("status") == attempt["status"]
            and payload.get("reason_code") == attempt["reason_code"]
            and payload.get("diff_sha256") == attempt["diff_sha256"]
            and payload.get("tests") == attempt["tests"]
        ):
            tests = attempt["tests"]
            if isinstance(tests, list) and len(tests) <= 16:
                return "reconciled_runner_receipt", tests
    return "none", []


def coding_evidence(connection: Connection, context: RunContext, arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = UUID(arguments["run_id"])
    result: dict[str, Any] = {
        "run_id": str(run_id),
        "found": False,
        "run_status": None,
        "attempts": [],
        "has_more": False,
    }
    run = (
        connection.execute(
            text(
                "SELECT status FROM runs WHERE project_id=:project_id AND id=:run_id AND mode='external_code'"
            ),
            {"project_id": str(context.project_id), "run_id": str(run_id)},
        )
        .mappings()
        .one_or_none()
    )
    if run is None:
        return result
    result["found"] = True
    result["run_status"] = run["status"]
    limit = max(0, min(context.max_items, 3))
    rows = (
        connection.execute(
            text(
                "SELECT a.id,a.adapter_id,a.fixture_id,a.status,a.reason_code,a.source_id,"
                "a.source_branch,a.source_revision,a.snapshot_source_id,a.snapshot_branch,"
                'a.snapshot_revision,a.snapshot_tree,a.baseline_commit,a."commit" AS candidate_commit,'
                "a.diff_sha256,"
                "a.receipt_sha256,a.tests FROM external_code_attempts a JOIN runs r "
                "ON r.project_id=a.project_id AND r.id=a.run_id "
                "WHERE a.project_id=:project_id AND a.run_id=:run_id AND r.mode='external_code' "
                "ORDER BY a.id LIMIT :limit"
            ),
            {"project_id": str(context.project_id), "run_id": str(run_id), "limit": limit + 1},
        )
        .mappings()
        .all()
    )
    result["has_more"] = len(rows) > limit
    for row in rows[:limit]:
        attempt = dict(row)
        if isinstance(attempt["tests"], str):
            attempt["tests"] = json.loads(attempt["tests"])
        source_kind = (
            "local"
            if attempt["fixture_id"] in ("local_source_probe_v1", "repository_code_v1")
            else "synthetic_github_snapshot"
            if attempt["fixture_id"] == "github_snapshot_codex_fixture_v1"
            else "fixture"
        )
        source_id = (
            attempt["source_id"]
            if source_kind == "local"
            else attempt["snapshot_source_id"]
            if source_kind == "synthetic_github_snapshot"
            else None
        )
        source_branch = (
            attempt["source_branch"]
            if source_kind == "local"
            else attempt["snapshot_branch"]
            if source_kind == "synthetic_github_snapshot"
            else None
        )
        source_revision = (
            attempt["source_revision"]
            if source_kind == "local"
            else attempt["snapshot_revision"]
            if source_kind == "synthetic_github_snapshot"
            else None
        )
        tests_source, tests = _receipt_tests(connection, context.project_id, run_id, attempt)
        verified = tests_source == "reconciled_runner_receipt"
        artifacts = (
            (
                connection.execute(
                    text(
                        "SELECT id,kind,sha256,byte_size FROM external_code_artifacts "
                        "WHERE project_id=:project_id AND run_id=:run_id AND attempt_id=:attempt_id "
                        "AND kind IN ('diff','log') "
                        "ORDER BY id LIMIT 2"
                    ),
                    {
                        "project_id": str(context.project_id),
                        "run_id": str(run_id),
                        "attempt_id": str(attempt["id"]),
                    },
                )
                .mappings()
                .all()
            )
            if verified
            else []
        )
        result["attempts"].append(
            {
                "attempt_id": str(attempt["id"]),
                "adapter_id": attempt["adapter_id"],
                "target_id": attempt["fixture_id"],
                "status": attempt["status"],
                "reason_code": attempt["reason_code"]
                if verified or attempt["status"] in {"queued", "unknown"}
                else None,
                "source_kind": source_kind,
                "source_id": str(source_id) if source_id is not None else None,
                "source_branch": source_branch,
                "source_revision": source_revision,
                "source_tree": attempt["snapshot_tree"]
                if source_kind == "synthetic_github_snapshot"
                else None,
                "baseline_commit": attempt["baseline_commit"] if verified else None,
                "candidate_commit": attempt["candidate_commit"] if verified else None,
                "diff_sha256": attempt["diff_sha256"] if verified else None,
                "receipt_sha256": attempt["receipt_sha256"] if verified else None,
                "receipt_event_verified": verified,
                "verified_outcome": attempt["status"]
                if verified and attempt["status"] in {"done", "failed"}
                else None,
                "tests_source": tests_source,
                "independent_qa_verified": False,
                "live_isolation_proven": False,
                "tests": tests,
                "artifacts": [
                    {
                        "artifact_id": str(a["id"]),
                        "kind": a["kind"],
                        "sha256": a["sha256"],
                        "byte_size": a["byte_size"],
                    }
                    for a in artifacts
                ],
            }
        )
    validate_coding_evidence(result)
    return result
