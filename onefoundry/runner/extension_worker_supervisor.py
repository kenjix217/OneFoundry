"""Separate host supervisor for one-shot extension jobs and durable recovery."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from runner.extension_worker_host import (
    MAX_JOB_BYTES,
    WorkerError,
    WorkerPolicy,
    _publish_private,
    _receipt,
    job_expired,
    process_job_file,
    validate_job,
)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        path.is_symlink()
        or not path.is_dir()
        or path.stat().st_uid != os.geteuid()
        or stat.S_IMODE(path.stat().st_mode) != 0o700
    ):
        raise WorkerError("worker_spool_not_private")


def _job(path: Path, policy: WorkerPolicy) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise WorkerError("worker_job_file_invalid")
    if path.stat().st_size > MAX_JOB_BYTES:
        raise WorkerError("worker_job_too_large")
    try:
        value = json.loads(path.read_bytes())
    except ValueError as exc:
        raise WorkerError("worker_job_json_invalid") from exc
    if not isinstance(value, dict) or path.name != f"{UUID(str(value.get('invocation_id')))}.json":
        raise WorkerError("worker_job_identity_invalid")
    validate_job(value, policy)
    return value


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    _publish_private(path, json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())


def _write_heartbeat(
    root: Path, policy: WorkerPolicy, supervisor_id: UUID, now: datetime | None = None
) -> None:
    """Replace one private capability record while the supervisor lock is held."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise WorkerError("worker_heartbeat_clock_invalid")
    heartbeat = {
        "schema_version": 1,
        "image_id": policy.image_id,
        "updated_at": current.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "supervisor_id": str(supervisor_id),
        "memory_mb": policy.memory_mb,
        "processes": policy.processes,
        "cpus": policy.cpus,
        "max_timeout_seconds": policy.max_timeout_seconds,
        "max_output_bytes": policy.max_output_bytes,
    }
    temporary = root / f".heartbeat-{uuid4()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(json.dumps(heartbeat, sort_keys=True, separators=(",", ":")).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / "heartbeat.json")
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def process_one(root: Path, policy: WorkerPolicy) -> dict[str, Any] | None:
    """Claim one exact job once. An interrupted claim becomes unknown, never replayed."""
    _private_directory(root)
    lock = os.open(root / ".attempts.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return _process_one_claimed(root, policy)
    finally:
        os.close(lock)


def _process_one_claimed(root: Path, policy: WorkerPolicy) -> dict[str, Any] | None:
    for name in ("jobs", "claims", "receipts", "cancellations", "callbacks/requests", "callbacks/responses"):
        _private_directory(root / name)
    for path in sorted((root / "jobs").glob("*.json")):
        try:
            UUID(path.stem)
        except ValueError:
            continue
        receipt_path = root / "receipts" / path.name
        if receipt_path.exists():
            continue
        job = _job(path, policy)
        claim_path = root / "claims" / path.name
        if claim_path.exists():
            receipt = _receipt(job, "unknown", reason="worker_interrupted_reconciliation_required")
            _write_receipt(receipt_path, receipt)
            return receipt
        if job_expired(job):
            receipt = _receipt(job, "failed", reason="worker_job_expired")
            _write_receipt(receipt_path, receipt)
            return receipt
        _publish_private(claim_path, str(job["job_sha256"]).encode())
        cancellation_path = root / "cancellations" / path.name
        if cancellation_path.exists():
            receipt = _receipt(job, "failed", reason="worker_cancelled_before_launch")
            _write_receipt(receipt_path, receipt)
            return receipt
        try:
            return process_job_file(path, root / "receipts", policy, root / "callbacks", cancellation_path)
        except (WorkerError, OSError, ValueError):
            receipt = _receipt(job, "unknown", reason="worker_supervisor_failure")
            if not receipt_path.exists():
                _write_receipt(receipt_path, receipt)
            return receipt
    return None


def _policy_from_config(path: Path) -> tuple[Path, WorkerPolicy, float]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise WorkerError("worker_config_not_private")
    value = json.loads(path.read_bytes())
    required = {
        "spool_root",
        "image_id",
        "docker_binary",
        "docker_socket",
        "memory_mb",
        "processes",
        "cpus",
        "max_timeout_seconds",
        "max_output_bytes",
        "poll_seconds",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise WorkerError("worker_config_invalid")
    root = Path(value["spool_root"])
    policy = WorkerPolicy(
        image_id=value["image_id"],
        docker_binary=Path(value["docker_binary"]),
        docker_socket=Path(value["docker_socket"]),
        memory_mb=value["memory_mb"],
        processes=value["processes"],
        cpus=value["cpus"],
        max_timeout_seconds=value["max_timeout_seconds"],
        max_output_bytes=value["max_output_bytes"],
    )
    poll = value["poll_seconds"]
    if not root.is_absolute() or type(poll) not in {int, float} or not 0.1 <= poll <= 10:
        raise WorkerError("worker_config_invalid")
    policy.validate()
    _private_directory(root)
    return root, policy, float(poll)


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated extension worker supervisor")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        root, policy, poll = _policy_from_config(args.config)
        lock = os.open(root / ".supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            supervisor_id = uuid4()
            if args.once:
                process_one(root, policy)
                return 0
            while True:
                _write_heartbeat(root, policy, supervisor_id)
                process_one(root, policy)
                time.sleep(poll)
        finally:
            os.close(lock)
    except (OSError, ValueError, WorkerError, BlockingIOError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
