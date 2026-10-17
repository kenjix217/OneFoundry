"""Trusted host handoff from a private browser spool volume to the static supervisor.

Only this host process can invoke Docker. Its transient volume helper is
networkless and cannot see the Docker socket; the browser child has no mounts.
After an exclusive claim, the host never launches that job a second time.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from runner.browser_static_job_spool import (
    MAX_MANIFEST,
    MAX_RECEIPT,
    MAX_SOURCE,
    BrowserStaticJobSpool,
    _canonical,
    _digest,
    _unique_keys,
)
from runner.browser_static_supervisor import run_static_browser_inspection

HELPER = Path(__file__).with_name("browser_static_volume_bridge.py").resolve()
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
VOLUME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
MAX_FRAME = 4 + MAX_MANIFEST + MAX_SOURCE + 4
PAGE_SIZE = 1000


class ClaimLost(Exception):
    """A timeout fence or another host process owns the browser job."""


class PoisonedJob(Exception):
    """A specific job failed validation before any browser launch."""


class TransientJob(Exception):
    """A job is still being atomically staged and may be seen on a later poll."""


def _id(raw: str) -> str:
    try:
        value = UUID(raw, version=4)
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid browser job ID") from error
    if str(value) != raw:
        raise ValueError("invalid browser job ID")
    return raw


def _directory(path: Path, *, mode: int = 0o700) -> None:
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("invalid host state path")
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != mode:
        raise ValueError("host state directory is not private")


def _read_private(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o137
            or not 1 <= info.st_size <= maximum
        ):
            raise ValueError("invalid host state file")
        data = os.read(descriptor, maximum + 1)
        if len(data) != info.st_size:
            raise ValueError("host state file changed")
        return data
    finally:
        os.close(descriptor)


def _docker(
    volume: str, image: str, action: str, job_id: str | None = None, transfer: Path | None = None
) -> bytes:
    if VOLUME.fullmatch(volume) is None:
        raise ValueError("invalid browser spool volume")
    if IMAGE.fullmatch(image) is None:
        raise ValueError("helper image must be an exact sha256 ID")
    if action not in {"list", "claim", "publish"}:
        raise ValueError("invalid browser helper action")
    if (action != "list" and job_id is None) or (action == "publish") != (transfer is not None):
        raise ValueError("invalid browser helper arguments")
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--user",
        f"10001:{os.getgid()}",
        "--mount",
        f"type=volume,src={volume},dst=/runner-spool",
        "--mount",
        f"type=bind,src={HELPER},dst=/bridge.py,readonly",
    ]
    if transfer is not None:
        if not transfer.is_absolute() or transfer.is_symlink() or not transfer.is_dir():
            raise ValueError("invalid browser transfer directory")
        command += ["--mount", f"type=bind,src={transfer},dst=/transfer,readonly"]
    command += [image, "python", "/bridge.py", action]
    if job_id is not None:
        command.append(_id(job_id))
    result = subprocess.run(command, capture_output=True, timeout=30, check=False)  # noqa: S603
    if result.returncode:
        if action == "claim" and result.returncode == 3:
            raise ClaimLost
        if action == "claim" and result.returncode == 4:
            raise PoisonedJob
        if action == "claim" and result.returncode == 5:
            raise TransientJob
        raise RuntimeError(f"browser volume bridge {action} failed (exit {result.returncode})")
    maximum = MAX_FRAME if action == "claim" else 37_000 if action == "list" else 0
    if len(result.stdout) > maximum:
        raise ValueError("browser volume bridge output too large")
    return result.stdout


def _decode_frame(raw: bytes, job_id: str) -> tuple[bytes, bytes, str]:
    if not 9 <= len(raw) <= MAX_FRAME:
        raise ValueError("invalid browser export frame")
    manifest_size = struct.unpack(">I", raw[:4])[0]
    if not 1 <= manifest_size <= MAX_MANIFEST or not 5 <= len(raw) - 4 - manifest_size <= MAX_SOURCE + 4:
        raise ValueError("invalid browser export frame")
    manifest_raw, source = raw[4 : 4 + manifest_size], raw[4 + manifest_size :]
    try:
        manifest = json.loads(manifest_raw, object_pairs_hook=_unique_keys)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid browser export frame") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("job_id") != job_id
        or manifest_raw != _canonical(manifest)
        or not isinstance(manifest.get("request_sha256"), str)
        or manifest["request_sha256"]
        != _digest(_canonical({key: value for key, value in manifest.items() if key != "request_sha256"}))
    ):
        raise ValueError("invalid browser export frame")
    return manifest_raw, source, manifest["request_sha256"]


def _local_spool(state_root: Path, job_id: str) -> BrowserStaticJobSpool:
    _directory(state_root)
    attempts = state_root / "attempts"
    _directory(attempts)
    path = attempts / _id(job_id)
    _directory(path)
    return BrowserStaticJobSpool(path)


def _purge_local_source(spool: BrowserStaticJobSpool, job_id: UUID) -> None:
    """Remove even a short write after a claimed local staging failure."""
    path = spool._path("sources", job_id, "bin")
    try:
        info = path.lstat()
    except FileNotFoundError:
        spool.purge_source(job_id)
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("invalid local browser source")
    if info.st_nlink == 2:
        spool.purge_source(job_id)
        return
    if info.st_nlink != 1:
        raise ValueError("invalid local browser source")
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    spool.purge_source(job_id)


def _purge_abandoned_sources(state_root: Path) -> None:
    attempts = state_root / "attempts"
    _directory(attempts)
    for path in sorted(attempts.iterdir()):
        job_id = _id(path.name)
        spool = _local_spool(state_root, job_id)
        _purge_local_source(spool, UUID(job_id))


@contextmanager
def _host_lock(state_root: Path) -> Iterator[None]:
    """Prevent a second host process from purging an active local source."""
    _directory(state_root)
    descriptor = os.open(state_root / ".bridge.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ValueError("invalid browser host lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _stage_transfer_receipt(target: Path, raw: bytes) -> None:
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target, follow_symlinks=False)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _poison_marker(state_root: Path, job_id: str) -> Path:
    directory = state_root / "poisoned"
    _directory(directory)
    return directory / f"{_id(job_id)}.json"


def _is_poisoned(state_root: Path, job_id: str) -> bool:
    marker = _poison_marker(state_root, job_id)
    try:
        content = _read_private(marker, 36)
    except FileNotFoundError:
        return False
    if content != job_id.encode("ascii"):
        raise ValueError("invalid poisoned browser job marker")
    return True


def _mark_poisoned(state_root: Path, job_id: str) -> None:
    marker = _poison_marker(state_root, job_id)
    try:
        _stage_transfer_receipt(marker, job_id.encode("ascii"))
    except FileExistsError:
        if not _is_poisoned(state_root, job_id):
            raise ValueError("poisoned browser job marker vanished") from None


def _publish_existing(args: argparse.Namespace, raw_id: str) -> None:
    job_id = _id(raw_id)
    spool = _local_spool(args.state_root, job_id)
    _purge_local_source(spool, UUID(job_id))
    receipt = spool.read_receipt(UUID(job_id))
    if receipt is None:
        raise ValueError("claimed browser job has no local receipt")
    raw = _read_private(spool.root / "receipts" / f"{job_id}.json", MAX_RECEIPT)
    transfer_root = args.state_root / "transfer"
    _directory(transfer_root)
    transfer = transfer_root / job_id
    _directory(transfer, mode=0o750)
    target = transfer / f"{job_id}.json"
    try:
        _stage_transfer_receipt(target, raw)
    except FileExistsError:
        if _read_private(target, MAX_RECEIPT) != raw:
            raise ValueError("conflicting browser transfer receipt") from None
    _docker(args.volume, args.helper_image, "publish", job_id, transfer)


def _run(args: argparse.Namespace) -> None:
    _directory(args.state_root)
    _purge_abandoned_sources(args.state_root)
    cursor: str | None = None
    while True:
        listed = _docker(args.volume, args.helper_image, "list", cursor).decode("ascii").splitlines()
        if len(listed) > PAGE_SIZE:
            raise ValueError("browser helper page too large")
        previous = cursor
        for raw_id in listed:
            job_id = _id(raw_id)
            if previous is not None and job_id <= previous:
                raise ValueError("browser helper page out of order")
            previous = job_id
            if _is_poisoned(args.state_root, job_id):
                continue
            try:
                frame = _docker(args.volume, args.helper_image, "claim", job_id)
            except (ClaimLost, TransientJob):
                continue
            except PoisonedJob:
                _mark_poisoned(args.state_root, job_id)
                print(f"poisoned browser static job {job_id}", file=sys.stderr)
                continue
            manifest, source, digest = _decode_frame(frame, job_id)
            spool = _local_spool(args.state_root, job_id)
            id_value = UUID(job_id)
            try:
                spool._new(spool._path("sources", id_value, "bin"), source)
                spool._new(spool._path("jobs", id_value, "json"), manifest)
                spool._new(spool._path("claims", id_value, "claim"), digest.encode("ascii"))
                spool.process_job(id_value, inspect=run_static_browser_inspection, preclaimed=True)
            finally:
                _purge_local_source(spool, id_value)
            _publish_existing(args, job_id)
            print(f"published browser static receipt {job_id}")
        if len(listed) < PAGE_SIZE:
            return
        cursor = previous


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted host bridge for static browser jobs")
    parser.add_argument("--volume", required=True)
    parser.add_argument("--helper-image", required=True)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--publish-existing")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    args = parser.parse_args()
    if not 1 <= args.poll_seconds <= 30:
        parser.error("--poll-seconds must be between 1 and 30")
    if args.publish_existing is not None and args.watch:
        parser.error("--publish-existing requires --once")
    try:
        with _host_lock(args.state_root):
            while True:
                if args.publish_existing is None:
                    _run(args)
                else:
                    _publish_existing(args, args.publish_existing)
                if args.once:
                    return
                time.sleep(args.poll_seconds)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"browser host bridge: {error}\n")


if __name__ == "__main__":
    main()
