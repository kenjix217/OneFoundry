"""Single-use spool consumer for the offline, fixed-fixture runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runner.contract import ContractError, Job, reject_duplicate_keys
from runner.fixture_git import Deadline, ExecutionError, StageOutput, UnknownOutcome, execute

ARTIFACT_NAMES = {
    "before_log": "before.log",
    "after_log": "after.log",
    "diff": "change.diff",
    "bundle": "repair.bundle",
    "qa_log": "qa.log",
    "merge_log": "merge.log",
}


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_new(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        _fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_manifest(path: Path) -> Job:
    if path.is_symlink():
        raise ContractError("manifest_symlink_forbidden")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(8193)
    except OSError as exc:
        raise ContractError("manifest_read_failed") from exc
    if len(raw) > 8192:
        raise ContractError("manifest_too_large")
    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContractError("invalid_manifest_json") from exc
    job = Job.parse(value)
    if path.name != f"{job.job_id}.json":
        raise ContractError("job_filename_mismatch")
    return job


def _write_artifacts(job: Job, spool: Path, output: StageOutput) -> list[dict[str, Any]]:
    directory = spool / "artifacts" / job.job_id
    directory.mkdir(parents=True, exist_ok=False)
    _fsync_dir(directory.parent)
    records: list[dict[str, Any]] = []
    for kind, data in output.artifacts.items():
        name = ARTIFACT_NAMES[kind]
        if len(data) > 65536:
            raise ExecutionError("artifact_limit_exceeded")
        relative = f"artifacts/{job.job_id}/{name}"
        _atomic_bytes(spool / relative, data)
        records.append(
            {
                "kind": kind,
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_size": len(data),
            }
        )
    return records


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def process_job(path: Path, spool: Path) -> dict[str, Any] | None:
    """Process one valid manifest at most once; a stale claim is never retried."""
    job = _read_manifest(path)
    receipt_path = spool / "receipts" / f"{job.job_id}.json"
    if receipt_path.exists():
        return None
    try:
        _atomic_new(spool / "claims" / f"{job.job_id}.claim", job.job_id.encode("ascii"))
    except FileExistsError:
        return None
    started_at = _utc_now()
    started = time.monotonic()
    output = StageOutput()
    status = "done"
    reason_code = "completed"
    artifacts: list[dict[str, Any]] = []
    execution_completed = False
    observed_head_commit: str | None = None
    try:
        output = execute(job, spool, Deadline(job.limits.wall_seconds))
        execution_completed = True
        artifacts = _write_artifacts(job, spool, output)
    except TimeoutError:
        status = "unknown"
        reason_code = "wall_time_exceeded"
    except UnknownOutcome as exc:
        status = "unknown"
        reason_code = exc.reason_code
        observed_head_commit = exc.observed_head_commit
    except (ExecutionError, OSError, subprocess.SubprocessError) as exc:
        status = "unknown" if job.stage == "merge" and execution_completed else "failed"
        reason_code = exc.reason_code if isinstance(exc, ExecutionError) else "runner_io_failed"
        if job.stage == "merge" and execution_completed:
            observed_head_commit = output.merged_commit
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job.job_id,
        "project_id": job.project_id,
        "run_id": job.run_id,
        "fixture_id": job.fixture_id,
        "stage": job.stage,
        "status": status,
        "reason_code": reason_code,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "branch": output.branch if status == "done" else None,
        "commit": output.commit if status == "done" else None,
        "baseline_commit": output.baseline_commit if status == "done" else None,
        "merged_commit": output.merged_commit if status == "done" else None,
        "observed_head_commit": output.merged_commit if status == "done" else observed_head_commit,
        "diff_sha256": output.diff_sha256 if status == "done" else None,
        "tests": output.tests if status == "done" else [],
        "artifacts": artifacts if status == "done" else [],
        "tokens_used": 0,
        "cost_usd": 0,
    }
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_new(receipt_path, raw)
    return receipt


def run_pending(spool: Path) -> int:
    jobs = spool / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    processed = 0
    for path in sorted(jobs.glob("*.json")):
        try:
            if process_job(path, spool) is not None:
                processed += 1
        except FileNotFoundError:
            # The Gateway may cancel an unclaimed job after this directory scan.
            continue
        except ContractError as exc:
            print(f"rejected manifest {path.name}: {exc}", file=sys.stderr)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline synthetic fixture runner")
    parser.add_argument("--spool", type=Path, default=Path("/runner-spool"))
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    spool: Path = arguments.spool
    if spool.is_symlink():
        parser.error("spool may not be a symlink")
    spool.mkdir(parents=True, exist_ok=True)
    if arguments.once:
        run_pending(spool)
        return
    while True:
        run_pending(spool)
        time.sleep(1)


if __name__ == "__main__":
    main()
