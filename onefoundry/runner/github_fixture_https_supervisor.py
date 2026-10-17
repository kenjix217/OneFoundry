"""Separate test-only supervisor for approved local HTTPS fixture verification."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from onefoundry.github_fixture_fetch_contract import FetchManifest, FetchReceipt, expiry
from onefoundry.github_stage_contract import canonical
from runner.contract import reject_duplicate_keys
from runner.github_https_fixture_fetch import FetchRequest, FixtureCredentialBroker, FixtureFetchError
from runner.local_source_stage import StageLimits
from runner.service import _atomic_new


class SupervisorError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HTTPSFixtureConfig:
    spool: Path
    endpoint: str
    ca_file: Path
    dummy_token: str
    limits: StageLimits

    def validate(self) -> None:
        if (
            not self.spool.is_absolute()
            or self.spool.is_symlink()
            or not self.spool.is_dir()
            or self.spool.resolve() != self.spool
            or not self.ca_file.is_absolute()
            or self.ca_file.is_symlink()
            or not self.ca_file.is_file()
        ):
            raise SupervisorError("fixture_https_config_invalid")
        for name in ("jobs", "claims", "receipts", "artifacts"):
            directory = self.spool / name
            if directory.is_symlink() or not directory.is_dir():
                raise SupervisorError("fixture_https_spool_invalid")
        try:
            FixtureCredentialBroker(self.endpoint, self.ca_file, self.dummy_token, self.limits)
        except (FixtureFetchError, OSError):
            raise SupervisorError("fixture_https_config_invalid") from None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _manifest(path: Path, spool: Path) -> FetchManifest:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise SupervisorError("fixture_manifest_path_invalid")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise SupervisorError("fixture_manifest_invalid")
            raw = os.read(fd, 8193)
            if len(raw) != info.st_size:
                raise SupervisorError("fixture_manifest_invalid")
        finally:
            os.close(fd)
        job = FetchManifest.model_validate(json.loads(raw, object_pairs_hook=reject_duplicate_keys))
    except (OSError, ValueError, UnicodeDecodeError, ValidationError):
        raise SupervisorError("fixture_manifest_invalid") from None
    if path.name != f"{job.job_id}.json":
        raise SupervisorError("fixture_manifest_identity_invalid")
    return job


def process_job(path: Path, config: HTTPSFixtureConfig) -> dict[str, Any] | None:
    config.validate()
    job = _manifest(path, config.spool)
    receipt_path = config.spool / "receipts" / f"{job.job_id}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        return None
    try:
        _atomic_new(config.spool / "claims" / f"{job.job_id}.claim", job.job_id.encode("ascii"))
    except FileExistsError:
        return None
    started_at = _now()
    status, reason = "unknown", "fixture_unavailable"
    revision: str | None = None
    tree: str | None = None
    files: int | None = None
    size: int | None = None
    try:
        if datetime.now(UTC) >= expiry(job.expires_at):
            raise SupervisorError("approval_expired")
        result = FixtureCredentialBroker(
            config.endpoint, config.ca_file, config.dummy_token, config.limits
        ).fetch(FetchRequest(job.source.owner, job.source.repo, job.source.branch, job.source.saved_revision))
        status, reason = result.status, result.reason_code
        revision, tree = result.source_revision, result.source_tree
        files, size = result.file_count, result.total_bytes
    except (FixtureFetchError, SupervisorError) as error:
        status, reason = "unknown", error.code
    except (OSError, ValueError):
        status, reason = "unknown", "fixture_unavailable"
    receipt = FetchReceipt.model_validate(
        {
            "schema_version": 2,
            "target_id": "github_fixture_https_v2",
            "action": job.action,
            "job_id": job.job_id,
            "project_id": job.project_id,
            "run_id": job.run_id,
            "approval_id": job.approval_id,
            "proposal_digest": job.proposal_digest,
            "source": job.source.model_dump(),
            "expires_at": job.expires_at,
            "idempotency_key": job.idempotency_key,
            "request_sha256": job.request_sha256,
            "status": status,
            "reason_code": reason,
            "source_revision": revision,
            "source_tree": tree,
            "file_count": files,
            "total_bytes": size,
            "started_at": started_at,
            "finished_at": _now(),
        }
    ).model_dump()
    _atomic_new(receipt_path, canonical(receipt) + b"\n")
    return receipt


def run_pending(config: HTTPSFixtureConfig) -> int:
    config.validate()
    count = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            if process_job(path, config) is not None:
                count += 1
        except (FileNotFoundError, SupervisorError):
            continue
    return count


def acquire_supervisor(config: HTTPSFixtureConfig) -> int:
    config.validate()
    try:
        fd = os.open(config.spool / ".supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SupervisorError("fixture_supervisor_lock_invalid")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
    except (OSError, BlockingIOError):
        raise SupervisorError("fixture_supervisor_already_running") from None
    return fd


def main() -> None:
    parser = argparse.ArgumentParser(description="Local HTTPS fixture verification supervisor")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--dummy-token-file", required=True, type=Path)
    parser.add_argument("--max-files", required=True, type=int)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--max-tree-bytes", required=True, type=int)
    parser.add_argument("--wall-seconds", required=True, type=float)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    token_file = args.dummy_token_file
    if token_file.is_symlink() or not token_file.is_file() or token_file.stat().st_mode & 0o077:
        parser.error("dummy token file must be private and regular")
    token = token_file.read_text().strip()
    config = HTTPSFixtureConfig(
        args.spool,
        args.endpoint,
        args.ca_file,
        token,
        StageLimits(args.max_files, args.max_bytes, args.max_tree_bytes, args.wall_seconds),
    )
    fd = acquire_supervisor(config)
    try:
        while True:
            run_pending(config)
            if args.once:
                return
            import time

            time.sleep(1)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except SupervisorError as error:
        print(error.code, file=sys.stderr)
        raise SystemExit(1) from None
