"""One-shot fake Codex worker for a v3 immutable snapshot manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.github_snapshot_codex_contract import SnapshotCodeManifest, expected_diff
from runner.contract import reject_duplicate_keys
from runner.docker_egress_proof import ProofError
from runner.external_cli import CliRequest
from runner.external_launch_journal import record_prelaunch, record_session
from runner.fixture_git import Deadline, _git, _run
from runner.github_fixture_snapshot_materialize import materialize_snapshot
from runner.github_snapshot_codex_fixture_proof import (
    AFTER,
    BEFORE,
    PROMPT,
    SnapshotCodexProofError,
    invoke_file_codex,
)
from runner.service import _atomic_bytes, _atomic_new

IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
LOG = re.compile(
    rb"synthetic_cli=codex_cli model=gpt-5 command=snapshot_file_repair "
    rb"image_sha256=sha256:[0-9a-f]{64} prompt_sha256=[0-9a-f]{64} "
    rb"allowed_provider_requests=2 blocked_other_connects=[0-9]{1,3}\n"
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _manifest(path: Path, spool: Path) -> SnapshotCodeManifest:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ValueError("snapshot_coding_manifest_path_invalid")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ValueError("snapshot_coding_manifest_invalid")
            raw = os.read(fd, 8193)
            if len(raw) != info.st_size:
                raise ValueError("snapshot_coding_manifest_invalid")
        finally:
            os.close(fd)
        value = SnapshotCodeManifest.model_validate(json.loads(raw, object_pairs_hook=reject_duplicate_keys))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError("snapshot_coding_manifest_invalid") from exc
    if path.name != f"{value.job_id}.json":
        raise ValueError("snapshot_coding_manifest_identity_invalid")
    return value


def _artifact(spool: Path, attempt: str, kind: str, name: str, data: bytes) -> dict[str, Any]:
    relative = f"artifacts/{attempt}/{name}"
    _atomic_bytes(spool / relative, data)
    return {
        "kind": kind,
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }


def process_snapshot_codex_job(
    path: Path, spool: Path, snapshot_root: Path, image_id: str, fixture_image_id: str
) -> dict[str, Any] | None:
    """Claim once and write a terminal receipt. No installed runner calls this automatically."""
    if IMAGE.fullmatch(image_id) is None or IMAGE.fullmatch(fixture_image_id) is None:
        raise ValueError("snapshot_coding_images_unpinned")
    job = _manifest(path, spool)
    attempt = str(job.attempt_id)
    if (spool / "receipts" / f"{attempt}.json").exists():
        return None
    try:
        _atomic_new(spool / "claims" / f"{attempt}.claim", attempt.encode())
    except FileExistsError:
        return None
    started = _now()
    status, reason = "unknown", "runner_failure_after_claim"
    session: str | None = None
    tokens: int | None = None
    baseline: str | None = None
    branch: str | None = None
    candidate: str | None = None
    diff_sha: str | None = None
    tests: list[dict[str, object]] = []
    artifacts: list[dict[str, Any]] = []
    try:
        deadline = Deadline(job.limits["wall_seconds"])
        with tempfile.TemporaryDirectory(prefix="onefoundry-snapshot-codex-work-") as temporary:
            work = Path(temporary)
            private_home = work / "private-home"
            private_home.mkdir(mode=0o700)
            with materialize_snapshot(
                snapshot_root,
                job.source.snapshot_path,
                job.source.snapshot_sha256,
                job.source.snapshot_byte_size,
                work,
            ) as materialized:
                repo = materialized.repository_path
                if (
                    materialized.source_tree != job.source.source_tree
                    or materialized.file_count != 1
                    or materialized.total_bytes != len(BEFORE)
                    or _git(repo, deadline, "ls-files").splitlines() != [b"file.txt"]
                    or (repo / "file.txt").is_symlink()
                    or (repo / "file.txt").read_bytes() != BEFORE
                ):
                    raise SnapshotCodexProofError("snapshot_source_unexpected")
                tests.append({"name": "file_text_before", "status": "failed", "exit_code": 1})
                _git(repo, deadline, "checkout", "-qb", "main")
                _git(repo, deadline, "commit", "-qm", "Synthetic baseline from fixture-asserted Git tree")
                baseline = _git(repo, deadline, "rev-parse", "HEAD").decode()
                if _git(repo, deadline, "rev-parse", "HEAD^{tree}").decode() != job.source.source_tree:
                    raise SnapshotCodexProofError("snapshot_baseline_tree_mismatch")
                branch = f"fixture/snapshot-{attempt}"
                _git(repo, deadline, "checkout", "-qb", branch)
                prompt = f"{job.task_title}\n\n{job.task_instructions}\n\n{PROMPT}\n"
                request = CliRequest(
                    "codex_cli",
                    Path("/opt/codex-cli/node_modules/.bin/codex"),
                    repo,
                    private_home,
                    prompt,
                    max(1, int(deadline.remaining())),
                    job.limits["max_log_bytes"],
                )
                payload = job.model_dump(mode="json")
                launch, launch_sha = record_prelaunch(
                    spool,
                    payload,
                    request,
                    baseline,
                    kind="synthetic_snapshot_codex_fixture",
                    image_id=image_id,
                )
                result = invoke_file_codex(request, image_id, fixture_image_id, attempt)
                record_session(spool, launch, launch_sha, result)
                session, tokens = result.provider_session_id, result.tokens_used
                if result.status != "done":
                    raise SnapshotCodexProofError(result.reason_code)
                if (
                    _git(repo, deadline, "rev-parse", "HEAD").decode() != baseline
                    or _run(("git", "status", "--porcelain", "--untracked-files=all"), repo, deadline).stdout
                    != b" M file.txt\n"
                    or (repo / "file.txt").is_symlink()
                    or (repo / "file.txt").read_bytes() != AFTER
                    or LOG.fullmatch(result.sanitized_log) is None
                    or tokens != 28
                    or result.cost_usd is not None
                    or not session
                ):
                    raise SnapshotCodexProofError("snapshot_coding_edit_unproved")
                tests.append({"name": "file_text_after", "status": "passed", "exit_code": 0})
                diff = _git(repo, deadline, "diff", "--", "file.txt") + b"\n"
                if len(diff) > job.limits["max_diff_bytes"] or diff != expected_diff():
                    raise SnapshotCodexProofError("snapshot_coding_diff_unproved")
                _git(repo, deadline, "add", "--", "file.txt")
                _git(repo, deadline, "commit", "-qm", "Synthetic fixture file repair")
                candidate = _git(repo, deadline, "rev-parse", "HEAD").decode()
                if _git(repo, deadline, "rev-parse", "HEAD^{tree}").decode() == job.source.source_tree:
                    raise SnapshotCodexProofError("snapshot_candidate_tree_unchanged")
                diff_sha = hashlib.sha256(diff).hexdigest()
                artifacts = [
                    _artifact(spool, attempt, "diff", "change.diff", diff),
                    _artifact(spool, attempt, "log", "after.log", result.sanitized_log),
                ]
                status, reason = "done", "completed"
    except (TimeoutError, OSError, ValueError, ProofError):
        status, reason = "unknown", "runner_failure_after_claim"
    if status != "done":
        branch = candidate = diff_sha = None
        artifacts = []
        tests = []
    receipt: dict[str, Any] = {
        "schema_version": 3,
        "job_id": attempt,
        "project_id": str(job.project_id),
        "run_id": str(job.run_id),
        "attempt_id": attempt,
        "adapter_id": "codex_cli",
        "fixture_id": None,
        "target_id": "github_snapshot_codex_fixture_v1",
        "source": None,
        "snapshot": job.source.model_dump(mode="json"),
        "idempotency_key": str(job.idempotency_key),
        "intent_sha256": job.intent_sha256,
        "request_sha256": job.request_sha256,
        "status": status,
        "reason_code": reason,
        "started_at": started,
        "finished_at": _now(),
        "provider_session_id": session,
        "baseline_commit": baseline,
        "branch": branch,
        "commit": candidate,
        "diff_sha256": diff_sha,
        "artifacts": artifacts,
        "tests": tests,
        "tokens_used": tokens,
        "cost_usd": None,
    }
    ExternalCodeReceipt.model_validate(receipt)
    _atomic_new(
        spool / "receipts" / f"{attempt}.json",
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return receipt
