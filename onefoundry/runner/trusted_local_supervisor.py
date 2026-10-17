"""Operator-host supervisor for one trusted Codex or Claude coding attempt at a time."""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
from pathlib import Path
from threading import Event, Thread

from runner.contract import ContractError
from runner.trusted_local_cli import (
    AuthReadinessCache,
    TrustedLocalConfig,
    TrustedLocalInvalid,
    process_trusted_local_job,
    write_trusted_capabilities,
)


def _lock(config: TrustedLocalConfig) -> int:
    config.validate()
    try:
        descriptor = os.open(
            config.spool / ".trusted-local.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise TrustedLocalInvalid("trusted_local_lock_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
    except (OSError, BlockingIOError):
        raise TrustedLocalInvalid("trusted_local_already_running") from None
    return descriptor


def run_pending(config: TrustedLocalConfig, *, ignored: set[tuple[str, int, int]] | None = None) -> int:
    """Process each valid pending job once; the attempt claim prevents replay."""
    config.validate()
    processed = 0
    rejected = ignored if ignored is not None else set()
    for path in sorted((config.spool / "jobs").glob("*.json")):
        attempt = path.stem
        if (config.spool / "claims" / f"{attempt}.claim").exists() or (
            config.spool / "receipts" / f"{attempt}.json"
        ).exists():
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        identity = (path.name, info.st_mtime_ns, info.st_size)
        if identity in rejected:
            continue
        try:
            if process_trusted_local_job(path, config) is not None:
                processed += 1
        except (ContractError, FileNotFoundError, TrustedLocalInvalid) as exc:
            rejected.add(identity)
            code = str(exc) if not isinstance(exc, FileNotFoundError) else "job_missing"
            print(f"trusted_local_job_skipped:{code}", file=sys.stderr)
    return processed


def _heartbeat(config: TrustedLocalConfig, stop: Event, seconds: float, failures: list[str]) -> None:
    auth_cache = AuthReadinessCache()
    while not stop.is_set():
        try:
            write_trusted_capabilities(config, auth_cache=auth_cache)
        except (OSError, TrustedLocalInvalid, ValueError):
            failures.append("trusted_local_capability_write_failed")
            return
        stop.wait(seconds)


def supervise(config: TrustedLocalConfig, *, once: bool, poll_seconds: float, job: Path | None) -> int:
    """Keep capability fresh while a possibly long CLI call is in progress."""
    if not 0.1 <= poll_seconds <= 60:
        raise TrustedLocalInvalid("trusted_local_poll_invalid")
    if job is not None and not once:
        raise TrustedLocalInvalid("trusted_local_job_requires_once")
    descriptor = _lock(config)
    stop = Event()
    failures: list[str] = []
    ignored: set[tuple[str, int, int]] = set()
    heartbeat: Thread | None = None
    try:
        if once:
            write_trusted_capabilities(config)
            if job is None:
                return run_pending(config, ignored=ignored)
            receipt = process_trusted_local_job(job, config)
            if receipt is None:
                raise TrustedLocalInvalid("trusted_local_claim_not_acquired")
            if receipt["status"] != "done":
                raise TrustedLocalInvalid(f"trusted_local_{receipt['reason_code']}")
            return 1
        heartbeat = Thread(target=_heartbeat, args=(config, stop, poll_seconds, failures), daemon=True)
        heartbeat.start()
        while True:
            if failures:
                raise TrustedLocalInvalid(failures[0])
            run_pending(config, ignored=ignored)
            if stop.wait(poll_seconds):
                return 0
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=config.auth_check_seconds * 2 + 12)
        try:
            write_trusted_capabilities(config, active=False)
        finally:
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted local Codex or Claude work-order supervisor")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--operator-home", required=True, type=Path)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--claude", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    arguments = parser.parse_args()
    config = TrustedLocalConfig(
        arguments.spool,
        arguments.operator_home,
        os.environ.get("PATH", ""),
        arguments.codex,
        arguments.claude,
    )
    try:
        supervise(config, once=arguments.once, poll_seconds=arguments.poll_seconds, job=arguments.job)
    except (TrustedLocalInvalid, ContractError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Provider errors can include protected context; the durable receipt is authoritative.
        print("trusted_local_supervisor_failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
