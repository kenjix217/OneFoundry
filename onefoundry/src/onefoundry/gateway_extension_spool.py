"""Private, bounded protocol files shared with the separate extension host."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from onefoundry.role_tool_admission import ToolDenied

if TYPE_CHECKING:
    from onefoundry.settings import Settings


def job_digest(job: dict[str, Any]) -> str:
    value = {key: item for key, item in job.items() if key != "job_sha256"}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ToolDenied("extension worker spool is not private")


def prepare_spool(settings: Settings) -> Path:
    base = Path(settings.runner_spool_root)
    if any(parent.is_symlink() for parent in (base, *base.parents)):
        raise ToolDenied("extension worker spool contains a symlink")
    root = base / "extensions"
    private_directory(root)
    for name in (
        "jobs",
        "claims",
        "receipts",
        "cancellations",
        "callbacks",
        "callbacks/requests",
        "callbacks/responses",
        "history",
    ):
        private_directory(root / name)
    return root


def read_json(path: Path, max_bytes: int) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > max_bytes
        ):
            raise ToolDenied("extension worker protocol file is invalid")
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ToolDenied("extension worker protocol file exceeds its bound")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ToolDenied("extension worker protocol must be a JSON object")
    return result


def publish_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    temporary = path.parent / f".{uuid4()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
