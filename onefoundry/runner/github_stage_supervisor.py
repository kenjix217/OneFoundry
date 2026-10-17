"""Separate, one-shot offline supervisor for one fixed synthetic local Git remote.

No token, GitHub URL, model, coding process, or network protocol is admitted.
The supervisor consumes a secret-free spool job and returns a receipt only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from onefoundry.github_stage_contract import StageManifest, StageReceipt, canonical
from onefoundry.source_github import REPO_PART
from runner.contract import reject_duplicate_keys
from runner.github_fixture_stage import stage_pinned_fixture
from runner.local_source_stage import StageError, StageLimits
from runner.service import _atomic_new

FIXTURE_MARKER = "onefoundry-synthetic-fixture.json"


class SupervisorError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OfflineConfig:
    spool: Path
    fixture_remote: Path
    fixture_owner: str
    fixture_repo: str
    stage_limits: StageLimits
    max_git_bytes: int

    def fixture_revision(self) -> str:
        marker = self.fixture_remote / FIXTURE_MARKER
        try:
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 512:
                    raise SupervisorError("offline_fixture_marker_invalid")
                raw = os.read(descriptor, 513)
                if len(raw) != info.st_size:
                    raise SupervisorError("offline_fixture_marker_invalid")
            finally:
                os.close(descriptor)
            value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        except (OSError, ValueError, UnicodeDecodeError):
            raise SupervisorError("offline_fixture_marker_invalid") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "owner", "repo", "revision"}
            or value["kind"] != "onefoundry_synthetic_github_fixture_v1"
            or value["owner"] != self.fixture_owner
            or value["repo"] != self.fixture_repo
            or not isinstance(value["revision"], str)
            or len(value["revision"]) != 40
            or any(char not in "0123456789abcdef" for char in value["revision"])
        ):
            raise SupervisorError("offline_fixture_marker_invalid")
        return value["revision"]

    def validate(self) -> None:
        self.stage_limits.validate()
        if (
            not self.spool.is_absolute()
            or self.spool.is_symlink()
            or not self.spool.is_dir()
            or self.spool.resolve() != self.spool
            or not self.fixture_remote.is_absolute()
            or self.fixture_remote.is_symlink()
            or not self.fixture_remote.is_dir()
            or self.fixture_remote.resolve() != self.fixture_remote
            or self.spool == self.fixture_remote
            or self.spool in self.fixture_remote.parents
            or self.fixture_remote in self.spool.parents
            or REPO_PART.fullmatch(self.fixture_owner) is None
            or REPO_PART.fullmatch(self.fixture_repo) is None
            or type(self.max_git_bytes) is not int
            or self.max_git_bytes < 1024
        ):
            raise SupervisorError("offline_fixture_config_invalid")
        for name in ("jobs", "claims", "receipts", "artifacts"):
            directory = self.spool / name
            if directory.is_symlink() or not directory.is_dir():
                raise SupervisorError("offline_spool_invalid")
        self.fixture_revision()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _manifest(path: Path, spool: Path) -> StageManifest:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise SupervisorError("stage_manifest_path_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise SupervisorError("stage_manifest_invalid")
            raw = os.read(descriptor, 8193)
            if len(raw) != info.st_size:
                raise SupervisorError("stage_manifest_invalid")
        finally:
            os.close(descriptor)
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        manifest = StageManifest.model_validate(value)
    except (OSError, ValueError, UnicodeDecodeError, ValidationError):
        raise SupervisorError("stage_manifest_invalid") from None
    if path.name != f"{manifest.job_id}.json":
        raise SupervisorError("stage_manifest_identity_invalid")
    return manifest


def process_job(path: Path, config: OfflineConfig) -> dict[str, Any] | None:
    """Claim once; failure after claim becomes a durable unknown receipt."""
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
    status, reason = "unknown", "offline_stage_unavailable"
    source_tree: str | None = None
    file_count: int | None = None
    total_bytes: int | None = None
    try:
        if (
            job.source.owner != config.fixture_owner
            or job.source.repo != config.fixture_repo
            or job.source.saved_revision != config.fixture_revision()
        ):
            raise SupervisorError("fixture_identity_mismatch")
        with tempfile.TemporaryDirectory(prefix="onefoundry-gh-stage-") as temp:
            result = stage_pinned_fixture(
                remote=config.fixture_remote,
                project_id=UUID(job.project_id),
                source_id=UUID(job.source.source_id),
                account_id=UUID(job.source.account_id),
                owner=job.source.owner,
                repo_name=job.source.repo,
                branch=job.source.branch,
                saved_revision=job.source.saved_revision,
                destination=Path(temp) / "staged",
                limits=config.stage_limits,
                max_git_bytes=config.max_git_bytes,
            )
            source_tree = result.source_tree
            file_count = result.file_count
            total_bytes = result.total_bytes
        status, reason = "done", "completed"
    except (StageError, SupervisorError, OSError, ValueError):
        status, reason = "unknown", "offline_stage_unavailable"
        source_tree = file_count = total_bytes = None
    receipt = StageReceipt.model_validate(
        {
            "schema_version": 1,
            "target_id": "github_stage_offline_v1",
            "job_id": job.job_id,
            "project_id": job.project_id,
            "run_id": job.run_id,
            "approval_id": job.approval_id,
            "proposal_digest": job.proposal_digest,
            "source": job.source.model_dump(),
            "request_sha256": job.request_sha256,
            "status": status,
            "reason_code": reason,
            "source_tree": source_tree,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "started_at": started_at,
            "finished_at": _now(),
        }
    ).model_dump()
    _atomic_new(receipt_path, canonical(receipt) + b"\n")
    return receipt


def run_pending(config: OfflineConfig) -> int:
    config.validate()
    count = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            if process_job(path, config) is not None:
                count += 1
        except (FileNotFoundError, SupervisorError):
            continue
    return count


def acquire_supervisor(config: OfflineConfig) -> int:
    """Hold one process-wide proof of an attached supervisor until descriptor close."""
    config.validate()
    try:
        descriptor = os.open(config.spool / ".supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SupervisorError("offline_supervisor_lock_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
    except (OSError, BlockingIOError):
        raise SupervisorError("offline_supervisor_already_running") from None
    return descriptor


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline synthetic GitHub stage supervisor")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--fixture-remote", required=True, type=Path)
    parser.add_argument("--fixture-owner", required=True)
    parser.add_argument("--fixture-repo", required=True)
    parser.add_argument("--max-files", required=True, type=int)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--max-tree-bytes", required=True, type=int)
    parser.add_argument("--wall-seconds", required=True, type=float)
    parser.add_argument("--max-git-bytes", required=True, type=int)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = OfflineConfig(
        args.spool,
        args.fixture_remote,
        args.fixture_owner,
        args.fixture_repo,
        StageLimits(args.max_files, args.max_bytes, args.max_tree_bytes, args.wall_seconds),
        args.max_git_bytes,
    )
    descriptor = acquire_supervisor(config)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            run_pending(config)
            if args.once:
                return
            import time

            time.sleep(1)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        main()
    except SupervisorError as error:
        print(error.code, file=sys.stderr)
        raise SystemExit(1) from None
