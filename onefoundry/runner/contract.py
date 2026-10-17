"""Strict wire contract for the isolated fixture runner."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

Stage = Literal["engineer", "qa", "merge"]
Status = Literal["done", "failed", "unknown"]
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ContractError(ValueError):
    """A manifest is outside the fixed offline fixture contract."""


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"invalid_{field}")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ContractError(f"invalid_{field}") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ContractError(f"invalid_{field}")
    return value


def _optional_uuid(value: object, field: str) -> str | None:
    return None if value is None else _uuid(value, field)


def _hex(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(f"invalid_{field}")
    return value


@dataclass(frozen=True)
class Limits:
    wall_seconds: int
    max_cost_usd: int
    max_log_bytes: int
    max_diff_bytes: int

    @classmethod
    def parse(cls, value: object) -> Limits:
        if not isinstance(value, dict) or set(value) != {
            "wall_seconds",
            "max_cost_usd",
            "max_log_bytes",
            "max_diff_bytes",
        }:
            raise ContractError("invalid_limits")
        wall = value["wall_seconds"]
        cost = value["max_cost_usd"]
        log = value["max_log_bytes"]
        diff = value["max_diff_bytes"]
        if type(wall) is not int or not 1 <= wall <= 60:
            raise ContractError("invalid_wall_seconds")
        if type(cost) is not int or cost != 0:
            raise ContractError("cost_limit_must_be_zero")
        if type(log) is not int or not 512 <= log <= 65536:
            raise ContractError("invalid_max_log_bytes")
        if type(diff) is not int or not 512 <= diff <= 65536:
            raise ContractError("invalid_max_diff_bytes")
        return cls(wall, cost, log, diff)


@dataclass(frozen=True)
class Job:
    schema_version: int
    job_id: str
    project_id: str
    run_id: str
    fixture_id: str
    stage: Stage
    actor_id: str
    source_job_id: str | None
    approval_id: str | None
    expected_branch: str | None
    expected_commit: str | None
    expected_diff_sha256: str | None
    expected_baseline_commit: str | None
    limits: Limits

    @classmethod
    def parse(cls, value: object) -> Job:
        fields = {
            "schema_version",
            "job_id",
            "project_id",
            "run_id",
            "fixture_id",
            "stage",
            "actor_id",
            "source_job_id",
            "approval_id",
            "expected_branch",
            "expected_commit",
            "expected_diff_sha256",
            "expected_baseline_commit",
            "limits",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ContractError("invalid_manifest_fields")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ContractError("unsupported_schema_version")
        if value["fixture_id"] != "engineer_qa_v1":
            raise ContractError("unsupported_fixture")
        stage = value["stage"]
        if stage not in ("engineer", "qa", "merge"):
            raise ContractError("invalid_stage")
        actor = value["actor_id"]
        if not isinstance(actor, str) or re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", actor) is None:
            raise ContractError("invalid_actor_id")
        source = _optional_uuid(value["source_job_id"], "source_job_id")
        approval = _optional_uuid(value["approval_id"], "approval_id")
        branch = value["expected_branch"]
        commit = value["expected_commit"]
        digest = value["expected_diff_sha256"]
        baseline = value["expected_baseline_commit"]
        if stage == "engineer":
            if any(item is not None for item in (source, approval, branch, commit, digest, baseline)):
                raise ContractError("engineer_requires_empty_source")
        else:
            if source is None or not isinstance(branch, str) or branch != f"fixture/repair-{source}":
                raise ContractError("invalid_expected_branch")
            _hex(commit, "expected_commit", HEX40)
            _hex(digest, "expected_diff_sha256", HEX64)
            _hex(baseline, "expected_baseline_commit", HEX40)
            if stage == "merge" and approval is None:
                raise ContractError("merge_requires_approval")
            if stage == "qa" and approval is not None:
                raise ContractError("qa_cannot_have_approval")
        return cls(
            schema_version=1,
            job_id=_uuid(value["job_id"], "job_id"),
            project_id=_uuid(value["project_id"], "project_id"),
            run_id=_uuid(value["run_id"], "run_id"),
            fixture_id="engineer_qa_v1",
            stage=stage,
            actor_id=actor,
            source_job_id=source,
            approval_id=approval,
            expected_branch=branch,
            expected_commit=commit,
            expected_diff_sha256=digest,
            expected_baseline_commit=baseline,
            limits=Limits.parse(value["limits"]),
        )


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result
