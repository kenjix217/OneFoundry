"""Small, fixed Docker-volume operations for the trusted host offline probe bridge.

This file runs only in a transient, networkless container started by the host
supervisor. It never launches the probe or receives credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from uuid import UUID, uuid4

SPOOL = Path("/runner-spool/external")
TRANSFER = Path("/transfer")
UUID4 = re.compile(r"[0-9a-f-]{36}\Z")
FILES = {"change.diff", "after.log"}


def _id(raw: str) -> str:
    if not UUID4.fullmatch(raw) or str(UUID(raw, version=4)) != raw:
        raise ValueError("invalid attempt ID")
    return raw


def _read(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid bridge file")
        data = os.read(fd, limit + 1)
        if len(data) != info.st_size:
            raise ValueError("bridge file changed")
        return data
    finally:
        os.close(fd)


def _new(path: Path, data: bytes) -> None:
    """Publish complete bytes under the final name in one exclusive step."""
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid4()}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_file(source: Path, target: Path, limit: int, digest: str) -> None:
    data = _read(source, limit)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("bridge digest mismatch")
    try:
        _new(target, data)
    except FileExistsError:
        if _read(target, limit) != data:
            raise ValueError("conflicting published file") from None


def claim(attempt: str) -> None:
    attempt = _id(attempt)
    manifest = _read(SPOOL / "jobs" / f"{attempt}.json", 8192)
    value = json.loads(manifest)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 2
        or value.get("attempt_id") != attempt
        or value.get("job_id") != attempt
        or value.get("adapter_id") != "offline_probe"
        or value.get("target_id") != "local_source_probe_v1"
    ):
        raise ValueError("not an offline probe manifest")
    if (SPOOL / "receipts" / f"{attempt}.json").exists():
        raise ValueError("receipt already exists")
    _new(SPOOL / "claims" / f"{attempt}.claim", attempt.encode("ascii"))
    sys.stdout.buffer.write(manifest)


def list_jobs() -> None:
    listed = 0
    for path in sorted((SPOOL / "jobs").glob("*.json")):
        try:
            attempt = _id(path.stem)
        except ValueError:
            continue
        if not (SPOOL / "claims" / f"{attempt}.claim").exists():
            print(attempt)
            listed += 1
            if listed >= 1000:
                return


def capability() -> None:
    data = _read(TRANSFER / "probe_capabilities.json", 4096)
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or type(value.get("available")) is not bool
        or type(value.get("isolated")) is not bool
        or not isinstance(value.get("observed_at"), str)
    ):
        raise ValueError("invalid capability")
    target = SPOOL / "probe_capabilities.json"
    temporary = SPOOL / f".probe_capabilities.{os.getpid()}.tmp"
    _new(temporary, data)
    os.replace(temporary, target)


def publish(attempt: str) -> None:
    attempt = _id(attempt)
    if _read(SPOOL / "claims" / f"{attempt}.claim", 64) != attempt.encode("ascii"):
        raise ValueError("canonical claim mismatch")
    local = TRANSFER / attempt
    if local.is_symlink() or not local.is_dir():
        raise ValueError("invalid transfer directory")
    receipt = _read(local / "receipt.json", 65536)
    value = json.loads(receipt)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 2
        or value.get("attempt_id") != attempt
        or value.get("job_id") != attempt
        or value.get("adapter_id") != "offline_probe"
        or value.get("target_id") != "local_source_probe_v1"
    ):
        raise ValueError("invalid probe receipt")
    manifest = json.loads(_read(SPOOL / "jobs" / f"{attempt}.json", 8192))
    if value.get("request_sha256") != manifest.get("request_sha256"):
        raise ValueError("receipt request mismatch")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > 2:
        raise ValueError("invalid artifact list")
    target_dir = SPOOL / "artifacts" / attempt
    target_dir.mkdir(mode=0o700, exist_ok=True)
    if target_dir.is_symlink() or not target_dir.is_dir():
        raise ValueError("invalid artifact directory")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("invalid artifact")
        name = str(artifact.get("path", "")).removeprefix(f"artifacts/{attempt}/")
        if name not in FILES or artifact.get("path") != f"artifacts/{attempt}/{name}":
            raise ValueError("invalid artifact path")
        digest = artifact.get("sha256")
        size = artifact.get("byte_size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid artifact digest")
        if type(size) is not int or not 0 <= size <= 65536:
            raise ValueError("invalid artifact size")
        source = local / name
        if source.is_symlink() or source.stat().st_size != size:
            raise ValueError("artifact size mismatch")
        _publish_file(source, target_dir / name, 65536, digest)
    _publish_file(
        local / "receipt.json",
        SPOOL / "receipts" / f"{attempt}.json",
        65536,
        hashlib.sha256(receipt).hexdigest(),
    )


def main() -> None:
    action = sys.argv[1]
    if action == "list" and len(sys.argv) == 2:
        list_jobs()
    elif action == "claim" and len(sys.argv) == 3:
        claim(sys.argv[2])
    elif action == "capability" and len(sys.argv) == 2:
        capability()
    elif action == "publish" and len(sys.argv) == 3:
        publish(sys.argv[2])
    else:
        raise ValueError("invalid bridge command")


if __name__ == "__main__":
    try:
        main()
    except FileExistsError:
        if len(sys.argv) > 1 and sys.argv[1] == "claim":
            raise SystemExit(3) from None
        raise
