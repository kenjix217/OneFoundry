"""Maintenance receipt lineage; provider prose is not a launch observation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.repository_code_contract import RepositoryCodeManifest
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid

if TYPE_CHECKING:
    from onefoundry.execution_gateway import ExecutionGateway


def saved_manifest(execution: ExecutionGateway, project_id: UUID, run_id: UUID) -> RepositoryCodeManifest:
    with execution.gateway.engine.connect() as connection:
        value = connection.scalar(
            text(
                "SELECT payload->'manifest' FROM events WHERE project_id=:p AND run_id=:r "
                "AND event_type='repository_code_queued' ORDER BY created_at,id LIMIT 1"
            ),
            {"p": project_id, "r": run_id},
        )
    return RepositoryCodeManifest.model_validate(value)


def validate_observation(
    execution: ExecutionGateway,
    project_id: UUID,
    run_id: UUID,
    attempt: dict[str, Any],
    receipt: ExternalCodeReceipt,
) -> None:
    job = saved_manifest(execution, project_id, run_id)
    if job.attempt_id != receipt.attempt_id or job.request_sha256 != receipt.request_sha256:
        raise ValueError("maintenance receipt differs from saved manifest")
    for suffix in ("repository-launch", "repository-session"):
        raw = execution.spool._read_regular(
            execution.spool.root / "claims" / f"{attempt['id']}.{suffix}.json", 4096
        )
        value = json.loads(raw, object_pairs_hook=RunnerSpool._no_duplicate_keys)
        if (
            value.get("attempt_id") != str(attempt["id"])
            or value.get("project_id") != str(project_id)
            or value.get("run_id") != str(run_id)
            or value.get("request_sha256") != receipt.request_sha256
        ):
            raise SpoolInvalid("maintenance launch observation identity changed")
        if suffix == "repository-launch" and value.get("baseline_commit") != receipt.baseline_commit:
            raise SpoolInvalid("maintenance baseline differs from launch")
        if suffix == "repository-session" and (
            value.get("provider_session_id") != receipt.provider_session_id or value.get("status") != "done"
        ):
            raise SpoolInvalid("maintenance provider terminal outcome is unproved")
    # Validate exact checkpoint checks, including order, to reject an omitted or
    # duplicated check even when a forged receipt contains plausible prose.
    names = [f"baseline_{i}" for i in range(len(job.target.test_commands))] + [
        f"candidate_{i}" for i in range(len(job.target.test_commands))
    ]
    if [test.name for test in receipt.tests] != names:
        raise ValueError("maintenance receipt omitted configured acceptance checks")
