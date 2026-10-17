"""Explicit offline spool supervisor for the fixed synthetic coding fixture.

This entrypoint is separate from the installed readiness observer. It receives
exact image IDs, owns the Docker-facing fixture bridges, and never publishes a
live-provider capability or supplies a credential from the host.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner.claude_workorder_fixture import process_synthetic_claude_job
from runner.codex_workorder_fixture import process_synthetic_codex_job
from runner.contract import ContractError
from runner.external_launch_journal import SHA256
from runner.external_service import _manifest


class OfflineSupervisorError(ValueError):
    """Invalid offline launch setup or job location, before a claim exists."""


@dataclass(frozen=True)
class OfflineSupervisorConfig:
    spool: Path
    fixture_image: str
    codex_image: str | None = None
    claude_image: str | None = None

    def validate(self) -> None:
        if (
            not self.spool.is_absolute()
            or self.spool.is_symlink()
            or not self.spool.is_dir()
            or self.spool.resolve() != self.spool
            or SHA256.fullmatch(self.fixture_image) is None
            or not (self.codex_image or self.claude_image)
            or any(
                image is not None and SHA256.fullmatch(image) is None
                for image in (self.codex_image, self.claude_image)
            )
        ):
            raise OfflineSupervisorError("offline_supervisor_config_invalid")
        for name in ("jobs", "claims", "receipts", "artifacts"):
            directory = self.spool / name
            if directory.is_symlink() or not directory.is_dir():
                raise OfflineSupervisorError("offline_supervisor_spool_invalid")


def process_job(path: Path, config: OfflineSupervisorConfig) -> dict[str, Any] | None:
    """Validate and route one exact manifest to its one-shot fixture bridge."""
    config.validate()
    if path.parent != config.spool / "jobs" or path.is_symlink():
        raise OfflineSupervisorError("offline_job_path_invalid")
    job = _manifest(path)
    adapter = job["adapter_id"]
    if adapter == "codex_cli" and config.codex_image is not None:
        return process_synthetic_codex_job(path, config.spool, config.codex_image, config.fixture_image)
    if adapter == "claude_code" and config.claude_image is not None:
        return process_synthetic_claude_job(path, config.spool, config.claude_image, config.fixture_image)
    raise OfflineSupervisorError("offline_adapter_unconfigured")


def run_pending(config: OfflineSupervisorConfig) -> int:
    """Consume valid pending jobs; invalid manifests stay unclaimed for review."""
    config.validate()
    processed = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            if process_job(path, config) is not None:
                processed += 1
        except (ContractError, FileNotFoundError, OfflineSupervisorError) as exc:
            # The manifest may be untrusted, so report only the stable error code.
            code = str(exc) if isinstance(exc, (ContractError, OfflineSupervisorError)) else "job_missing"
            print(f"offline_job_skipped:{code}", file=sys.stderr)
            continue
    return processed


def acquire_supervisor(config: OfflineSupervisorConfig) -> int:
    """Keep only one polling supervisor on this spool."""
    config.validate()
    try:
        descriptor = os.open(
            config.spool / ".offline-supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OfflineSupervisorError("offline_supervisor_lock_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
    except (OSError, BlockingIOError):
        raise OfflineSupervisorError("offline_supervisor_already_running") from None
    return descriptor


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit offline coding fixture supervisor")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--fixture-image", required=True)
    parser.add_argument("--codex-image")
    parser.add_argument("--claude-image")
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    config = OfflineSupervisorConfig(
        arguments.spool,
        arguments.fixture_image,
        arguments.codex_image,
        arguments.claude_image,
    )
    descriptor = acquire_supervisor(config)
    try:
        while True:
            run_pending(config)
            if arguments.once:
                return
            time.sleep(1)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        main()
    except OfflineSupervisorError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
