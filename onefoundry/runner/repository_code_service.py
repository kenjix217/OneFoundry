"""One-shot disposable maintenance worker, independent of the app process.

The caller must supply a provider bridge which owns OS/network admission. This
module never invokes a coding CLI directly or turns a trusted-home CLI into an
isolated capability. The offline bridge is deliberately not a live capability.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.repository_code_contract import RepositoryCodeManifest
from onefoundry.repository_source_safety import observe
from onefoundry.runner_spool import REASON, RunnerSpool
from onefoundry.workflow_schemas import SECRET_PATTERN
from runner.external_cli import CliResult
from runner.fixture_git import Deadline, ExecutionError
from runner.isolated_fixture_test import run_isolated_fixture_test
from runner.local_source_stage import StageError
from runner.repository_candidate_observation import CandidateRejected, validated
from runner.repository_check_evidence import check_log
from runner.repository_code_stage import candidate, stage
from runner.repository_transport import publish_new, publish_replace

ProviderBridge = Callable[[RepositoryCodeManifest, Path, Deadline], CliResult]
TestRunner = Callable[..., tuple[dict[str, Any], bytes]]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def process_job(
    path: Path,
    spool: Path,
    source_root: Path,
    provider: ProviderBridge,
    *,
    test_runner: TestRunner = run_isolated_fixture_test,
) -> dict[str, Any] | None:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ValueError("maintenance_job_path_invalid")
    job = RepositoryCodeManifest.model_validate_json(RunnerSpool._read_regular(path, 8192))
    attempt = str(job.attempt_id)
    if path.name != f"{attempt}.json":
        raise ValueError("maintenance_job_identity_invalid")
    if (spool / "receipts" / f"{attempt}.json").exists():
        return None
    try:
        publish_new(spool / "claims" / f"{attempt}.claim", attempt.encode())
    except FileExistsError:
        return None
    started = _now()
    status, reason = "failed", "maintenance_preflight_failed"
    launched = False
    result: CliResult | None = None
    baseline: str | None = None
    commit: str | None = None
    diff: bytes | None = None
    tests: list[dict[str, Any]] = []
    check_outputs: list[tuple[str, str, bytes]] = []
    artifacts: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-maintenance-") as temporary:
            work = Path(temporary)
            repo = work / "repo"
            source, baseline = stage(job, source_root, repo)
            deadline = Deadline(job.limits["wall_seconds"])
            for index, argv in enumerate(job.target.test_commands):
                check, _ = test_runner(
                    repo,
                    deadline,
                    job.limits["max_log_bytes"],
                    "before",
                    image_id=job.target.test_image,
                    command=tuple(argv),
                )
                tests.append({**check, "name": f"baseline_{index}"})
            if not any(item["status"] == "failed" for item in tests):
                raise StageError("maintenance_gap_not_reproduced")
            admit_context = getattr(provider, "validate_context", None)
            if admit_context is not None:
                admit_context(job, repo)
            publish_new(
                spool / "claims" / f"{attempt}.repository-launch.json",
                json.dumps(
                    {
                        "attempt_id": attempt,
                        "project_id": str(job.project_id),
                        "run_id": str(job.run_id),
                        "request_sha256": job.request_sha256,
                        "source_tree": source.source_tree,
                        "baseline_commit": baseline,
                        "prepared_at": _now(),
                    },
                    sort_keys=True,
                ).encode(),
            )
            # The bridge receives only the disposable tree. It must keep .git,
            # receipts and credentials outside the coding process mount.
            launched = True
            result = validated(provider(job, repo, deadline))
            if result.status != "done" or not result.provider_session_id:
                status, reason = result.status, result.reason_code
                raise ValueError(reason)
            for index, argv in enumerate(job.target.test_commands):
                check, output = test_runner(
                    repo,
                    deadline,
                    job.limits["max_log_bytes"],
                    "after",
                    image_id=job.target.test_image,
                    command=tuple(argv),
                )
                tests.append({**check, "name": f"candidate_{index}"})
                check_outputs.append((f"candidate_{index}", check["status"], output))
            commit, diff = candidate(job, repo, baseline, deadline)
            publish_new(
                spool / "claims" / f"{attempt}.source-safety.json", observe(job, repo, baseline, commit, diff)
            )
            log = result.sanitized_log
            if len(log) > job.limits["max_log_bytes"] or SECRET_PATTERN.search(log.decode("utf-8")):
                raise StageError("maintenance_log_invalid")
            log = check_log(check_outputs, log, job.limits["max_log_bytes"])
            for kind, name, data in (("diff", "change.diff", diff), ("log", "after.log", log)):
                relative = f"artifacts/{attempt}/{name}"
                publish_replace(spool / relative, data)
                artifacts.append(
                    {
                        "kind": kind,
                        "path": relative,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "byte_size": len(data),
                    }
                )
            if all(item["status"] == "passed" for item in tests if item["name"].startswith("candidate_")):
                status, reason = "done", "completed"
            else:
                # A completed, scoped patch with known failing checks is useful
                # repair evidence. Its failed status forbids QA/release admission.
                status, reason = "failed", "maintenance_candidate_checks_failed"
    except (OSError, TimeoutError, ValueError, RuntimeError, ExecutionError) as exc:
        if isinstance(exc, CandidateRejected):
            result = exc.observation
        if status != "done":
            status = "unknown" if launched and (result is None or result.status == "unknown") else "failed"
            if reason == "maintenance_preflight_failed" and launched:
                reason = (
                    "maintenance_candidate_failed"
                    if result is not None
                    else "maintenance_provider_outcome_unknown"
                )
            commit, diff, artifacts = None, None, []
            if isinstance(exc, StageError):
                reason = exc.code
            elif isinstance(exc, ExecutionError):
                reason = (
                    exc.reason_code
                    if REASON.fullmatch(exc.reason_code)
                    else "maintenance_execution_owner_failed"
                )
    # A bridge observation is persisted separately from its candidate receipt.
    # Offline results remain offline; the Gateway does not grant live readiness.
    if result is not None:
        publish_new(
            spool / "claims" / f"{attempt}.repository-session.json",
            json.dumps(
                {
                    "attempt_id": attempt,
                    "project_id": str(job.project_id),
                    "run_id": str(job.run_id),
                    "request_sha256": job.request_sha256,
                    "provider_session_id": result.provider_session_id,
                    "status": result.status,
                    "reason_code": result.reason_code,
                    "tokens_used": result.tokens_used,
                    "diagnostics": _diagnostics(result.sanitized_log),
                    "observed_at": _now(),
                },
                sort_keys=True,
            ).encode(),
        )
    receipt = {
        "schema_version": 4,
        "job_id": attempt,
        "project_id": str(job.project_id),
        "run_id": str(job.run_id),
        "attempt_id": attempt,
        "adapter_id": job.adapter_id,
        "target_id": "repository_code_v1",
        "source": job.source,
        "idempotency_key": str(job.idempotency_key),
        "intent_sha256": job.intent_sha256,
        "request_sha256": job.request_sha256,
        "status": status,
        "reason_code": reason,
        "started_at": started,
        "finished_at": _now(),
        "provider_session_id": result.provider_session_id if result is not None else None,
        "baseline_commit": baseline,
        "branch": f"onefoundry/extension-{attempt}" if commit else None,
        "commit": commit,
        "diff_sha256": hashlib.sha256(diff).hexdigest() if diff else None,
        "artifacts": artifacts,
        "tests": tests,
        "tokens_used": result.tokens_used if result is not None else None,
        "cost_usd": str(result.cost_usd) if result is not None and result.cost_usd is not None else None,
    }
    ExternalCodeReceipt.model_validate(receipt)
    publish_new(spool / "receipts" / f"{attempt}.json", (json.dumps(receipt, sort_keys=True) + "\n").encode())
    return receipt


def _diagnostics(raw: bytes) -> list[dict[str, Any]]:
    """Re-validate metadata before persisting an untrusted bridge's observation."""
    from runner.repository_codex_observation import result as observe_result

    if len(raw) > 4096:
        return []
    validated = observe_result(raw).sanitized_log
    return [json.loads(line) for line in validated.splitlines()]
