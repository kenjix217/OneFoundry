"""Explicit host-only fake snapshot Codex supervisor; never installed in the app."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runner import codex_broker_proof as broker_proof
from runner import docker_egress_proof as egress
from runner.github_snapshot_codex_service import IMAGE, _manifest, process_snapshot_codex_job
from runner.service import _atomic_bytes, _atomic_new


class SnapshotCodexSupervisorError(ValueError):
    """The fake-only worker cannot safely advertise or consume jobs."""


@dataclass(frozen=True)
class SnapshotCodexConfig:
    spool: Path
    snapshot_root: Path
    image_id: str
    fixture_image_id: str
    claim_timeout_seconds: int

    def validate(self) -> None:
        if (
            not self.spool.is_absolute()
            or not self.snapshot_root.is_absolute()
            or self.spool.is_symlink()
            or self.snapshot_root.is_symlink()
            or not self.spool.is_dir()
            or not self.snapshot_root.is_dir()
            or self.spool.resolve() != self.spool
            or self.snapshot_root.resolve() != self.snapshot_root
            or self.spool == self.snapshot_root
            or self.spool in self.snapshot_root.parents
            or self.snapshot_root in self.spool.parents
            or IMAGE.fullmatch(self.image_id) is None
            or IMAGE.fullmatch(self.fixture_image_id) is None
            or not 1 <= self.claim_timeout_seconds <= 3600
        ):
            raise SnapshotCodexSupervisorError("snapshot_codex_config_invalid")
        for directory in (
            self.snapshot_root / "snapshots",
            *(self.spool / name for name in ("jobs", "claims", "receipts", "artifacts")),
        ):
            if directory.is_symlink() or not directory.is_dir():
                raise SnapshotCodexSupervisorError("snapshot_codex_storage_invalid")


def _capability(config: SnapshotCodexConfig) -> None:
    proof = broker_proof.prove_broker_turn(config.image_id, config.fixture_image_id)
    if (
        proof.get("image_id") != config.image_id
        or proof.get("fixture_image_id") != config.fixture_image_id
        or proof.get("provider_requests") != 2
        or proof.get("reported_tokens") != 28
        or proof.get("cost_usd") is not None
    ):
        raise SnapshotCodexSupervisorError("snapshot_codex_broker_proof_invalid")
    payload = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "available": True,
        "synthetic_only": True,
        "brokered": True,
        "isolated": True,
    }
    _atomic_bytes(
        config.spool / "snapshot_codex_capabilities.json",
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    )


def run_pending(config: SnapshotCodexConfig) -> int:
    config.validate()
    _capability(config)
    count = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            if (
                process_snapshot_codex_job(
                    path, config.spool, config.snapshot_root, config.image_id, config.fixture_image_id
                )
                is not None
            ):
                count += 1
        except (OSError, ValueError):
            # Other job kinds and malformed manifests are not authorized here.
            continue
    fence_orphans(config)
    return count


def fence_orphans(config: SnapshotCodexConfig) -> int:
    """After a timed claim, stop exact attempt-named Docker children before a terminal unknown."""
    count = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            job = _manifest(path, config.spool)
            attempt = str(job.attempt_id)
            claim = config.spool / "claims" / f"{attempt}.claim"
            info = claim.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or time.time() - info.st_mtime < config.claim_timeout_seconds
                or (config.spool / "claims" / f"{attempt}.fenced.json").exists()
            ):
                continue
            suffix = job.attempt_id.hex
            names = {
                f"ap-snapshot-server-{suffix}",
                f"ap-snapshot-broker-{suffix}",
                f"ap-snapshot-cli-{suffix}",
            }
            observed = set(egress._docker("ps", "-a", "--format", "{{.Names}}").splitlines())
            active = sorted(names & observed)
            if active:
                egress._docker("rm", "-f", *active)
            remaining = set(egress._docker("ps", "-a", "--format", "{{.Names}}").splitlines())
            if names & remaining:
                continue
            fence = {
                "schema_version": 1,
                "attempt_id": attempt,
                "project_id": str(job.project_id),
                "run_id": str(job.run_id),
                "request_sha256": job.request_sha256,
                "reason_code": "claimed_receipt_missing",
                "inspected_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            _atomic_new(
                config.spool / "claims" / f"{attempt}.fenced.json",
                json.dumps(fence, sort_keys=True, separators=(",", ":")).encode(),
            )
            count += 1
        except (OSError, ValueError, egress.ProofError):
            continue
    return count


def acquire_supervisor(config: SnapshotCodexConfig) -> int:
    config.validate()
    try:
        fd = os.open(config.spool / ".snapshot-codex.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SnapshotCodexSupervisorError("snapshot_codex_lock_invalid")
            os.fchmod(fd, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
    except (OSError, BlockingIOError):
        raise SnapshotCodexSupervisorError("snapshot_codex_already_running") from None
    return fd


def main() -> None:
    parser = argparse.ArgumentParser(description="Host-only synthetic snapshot Codex runner")
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--fixture-image-id", required=True)
    parser.add_argument("--claim-timeout-seconds", type=int, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = SnapshotCodexConfig(
        args.spool, args.snapshot_root, args.image_id, args.fixture_image_id, args.claim_timeout_seconds
    )
    fd = acquire_supervisor(config)
    try:
        while True:
            run_pending(config)
            if args.once:
                return
            time.sleep(1)
    finally:
        (config.spool / "snapshot_codex_capabilities.json").unlink(missing_ok=True)
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except SnapshotCodexSupervisorError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
