"""Host-only bridge between OneFoundry's named spool volume and the offline probe.

Run as the trusted host operator. Docker is invoked here only; neither the app
nor the isolated probe child receives its socket or a volume mount.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

HELPER = Path(__file__).with_name("probe_volume_bridge.py").resolve()
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


class ClaimLost(Exception):
    """Another supervisor already claimed this attempt."""


def _directory(path: Path, mode: int = 0o700) -> None:
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("invalid private state path")
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("invalid private state directory")
    path.chmod(mode)


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _read_private(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid local bridge file")
        data = os.read(fd, limit + 1)
        if len(data) != info.st_size:
            raise ValueError("local bridge file changed")
        return data
    finally:
        os.close(fd)


def _attempt(raw: str) -> str:
    if str(UUID(raw, version=4)) != raw:
        raise ValueError("invalid attempt ID")
    return raw


def _docker(
    volume: str, image: str, action: str, attempt: str | None = None, transfer: Path | None = None
) -> bytes:
    if not NAME.fullmatch(volume) or not NAME.fullmatch(image):
        raise ValueError("invalid Docker volume or image name")
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
        command += ["--mount", f"type=bind,src={transfer},dst=/transfer,readonly"]
    command += [image, "python", "/bridge.py", action]
    if attempt is not None:
        command.append(_attempt(attempt))
    result = subprocess.run(command, capture_output=True, timeout=30, check=False)  # noqa: S603 - validated names, fixed CLI
    if result.returncode != 0:
        if action == "claim" and result.returncode == 3:
            raise ClaimLost
        raise RuntimeError(f"volume bridge {action} failed (exit {result.returncode})")
    if len(result.stdout) > (8192 if action == "claim" else 65536):
        raise ValueError("volume bridge output too large")
    return result.stdout


def _supervisor(args: argparse.Namespace, spool: Path, attempt: str | None = None) -> None:
    command = [
        sys.executable,
        "-m",
        "runner.local_source_probe",
        "--spool",
        str(spool),
        "--admitted-root",
        str(args.admitted_root),
        "--bwrap",
        str(args.bwrap),
        "--stage-max-files",
        str(args.stage_max_files),
        "--stage-max-bytes",
        str(args.stage_max_bytes),
        "--stage-max-tree-bytes",
        str(args.stage_max_tree_bytes),
        "--stage-wall-seconds",
        str(args.stage_wall_seconds),
        "--once",
    ]
    if attempt is not None:
        command += ["--preclaimed-attempt", attempt]
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(HELPER.parent.parent),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(args.state_root),
        "LC_ALL": "C.UTF-8",
    }
    result = subprocess.run(  # noqa: S603 - fixed module and bounded arguments
        command, cwd=HELPER.parent.parent, env=env, timeout=90, check=False
    )
    if result.returncode:
        raise RuntimeError(f"offline probe supervisor failed (exit {result.returncode})")


def _transfer_file(source: Path, target: Path, limit: int) -> None:
    data = _read_private(source, limit)
    _write_private(target, data, 0o640)


def _publish_capability(args: argparse.Namespace, source: Path) -> None:
    transfer = args.state_root / "transfer-capability"
    _directory(transfer, 0o750)
    target = transfer / "probe_capabilities.json"
    target.unlink(missing_ok=True)
    _transfer_file(source, target, 4096)
    _docker(args.volume, args.image, "capability", transfer=transfer)


def _capability_ready(source: Path) -> bool:
    try:
        value = json.loads(_read_private(source, 4096))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("isolated") is True
        and value.get("source_root_ready") is True
        and value.get("available") is True
        and value.get("reason") is None
    )


def _publish_stopped(args: argparse.Namespace) -> None:
    transfer = args.state_root / "transfer-stopped"
    _directory(transfer, 0o750)
    target = transfer / "probe_capabilities.json"
    temporary = transfer / ".probe_capabilities.tmp"
    temporary.unlink(missing_ok=True)
    payload = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "isolated": False,
        "source_root_ready": False,
        "available": False,
        "reason": "supervisor_stopped",
    }
    _write_private(temporary, (json.dumps(payload, sort_keys=True) + "\n").encode(), 0o640)
    os.replace(temporary, target)
    _docker(args.volume, args.image, "capability", transfer=transfer)


def _publish_attempt(args: argparse.Namespace, attempt: str, spool: Path) -> None:
    transfer = args.state_root / "transfer-attempts"
    _directory(transfer, 0o750)
    target = transfer / attempt
    _directory(target, 0o750)
    receipt = spool / "receipts" / f"{attempt}.json"
    if not receipt.is_file():
        raise RuntimeError(f"claimed attempt {attempt} has no local receipt; inspect state before recovery")
    for name, source, limit in (
        ("receipt.json", receipt, 65536),
        ("change.diff", spool / "artifacts" / attempt / "change.diff", 65536),
        ("after.log", spool / "artifacts" / attempt / "after.log", 65536),
    ):
        destination = target / name
        destination.unlink(missing_ok=True)
        if source.exists():
            _transfer_file(source, destination, limit)
    _docker(args.volume, args.image, "publish", attempt, transfer)


def _run(args: argparse.Namespace) -> None:
    if (
        not args.state_root.is_absolute()
        or args.state_root.is_symlink()
        or args.state_root.resolve() != args.state_root
    ):
        raise ValueError("invalid private state path")
    _directory(args.state_root)
    if not args.admitted_root.is_absolute() or not args.admitted_root.is_dir():
        raise ValueError("admitted source root unavailable")
    if not args.bwrap.is_file():
        raise ValueError("bwrap unavailable")
    capability_spool = args.state_root / "capability"
    _directory(capability_spool)
    for name in ("jobs", "claims", "receipts", "artifacts"):
        _directory(capability_spool / name)
    _supervisor(args, capability_spool)
    _publish_capability(args, capability_spool / "probe_capabilities.json")
    if args.publish_existing is not None:
        attempt = _attempt(args.publish_existing)
        _publish_attempt(args, attempt, args.state_root / "attempts" / attempt)
        return
    if not _capability_ready(capability_spool / "probe_capabilities.json"):
        return
    ids = _docker(args.volume, args.image, "list").decode("ascii").splitlines()
    for raw in ids:
        attempt = _attempt(raw)
        try:
            manifest = _docker(args.volume, args.image, "claim", attempt)
        except ClaimLost:
            continue  # Another supervisor may have won the exclusive claim.
        spool = args.state_root / "attempts" / attempt
        _directory(spool)
        for name in ("jobs", "claims", "receipts", "artifacts"):
            _directory(spool / name)
        _write_private(spool / "jobs" / f"{attempt}.json", manifest)
        _write_private(spool / "claims" / f"{attempt}.claim", attempt.encode("ascii"))
        _supervisor(args, spool, attempt)
        _publish_capability(args, spool / "probe_capabilities.json")
        _publish_attempt(args, attempt, spool)
        print(f"published offline probe receipt {attempt}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted host bridge for the OneFoundry offline probe")
    parser.add_argument("--volume", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--admitted-root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--bwrap", default=Path("/usr/bin/bwrap"), type=Path)
    parser.add_argument("--stage-max-files", required=True, type=int)
    parser.add_argument("--stage-max-bytes", required=True, type=int)
    parser.add_argument("--stage-max-tree-bytes", required=True, type=int)
    parser.add_argument("--stage-wall-seconds", required=True, type=float)
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
        while True:
            _run(args)
            if args.once:
                return
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        if args.watch:
            try:
                _publish_stopped(args)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                print("host bridge: stop capability could not be published; it will expire", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"host bridge: {exc}\n")


if __name__ == "__main__":
    main()
