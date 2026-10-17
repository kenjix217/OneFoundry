"""Attempt-bound, append-only public progress journal in the external claim spool."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from runner.provider_progress import MAX_PROGRESS, ProviderProgress, admit_progress

MAX_JOURNAL_BYTES = 65536
MAX_RECORD_BYTES = 2048
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ProgressSpoolInvalid(ValueError):
    """A stable, nonsecret progress journal failure."""


@dataclass(frozen=True)
class ProgressRecord:
    sequence: int
    progress: ProviderProgress


@dataclass(frozen=True)
class ProgressSnapshot:
    records: tuple[ProgressRecord, ...]
    latest_sequence: int


def _identity(project_id: str, run_id: str, attempt_id: str, request_sha256: str) -> dict[str, str]:
    try:
        if any(str(UUID(value)) != value for value in (project_id, run_id, attempt_id)):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ProgressSpoolInvalid("progress_identity_invalid") from None
    if not isinstance(request_sha256, str) or SHA256.fullmatch(request_sha256) is None:
        raise ProgressSpoolInvalid("progress_identity_invalid")
    return {
        "project_id": project_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "request_sha256": request_sha256,
    }


def _claims(spool: Path) -> int:
    if not spool.is_absolute() or spool.is_symlink() or not spool.is_dir():
        raise ProgressSpoolInvalid("progress_directory_invalid")
    try:
        descriptor = os.open(spool / "claims", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            raise ProgressSpoolInvalid("progress_directory_invalid")
        return descriptor
    except OSError:
        raise ProgressSpoolInvalid("progress_directory_invalid") from None


def _checked_file(
    directory: int, name: str, flags: int, *, owner_uid: int | None = None, group_readable: bool = False
) -> int:
    try:
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        raise ProgressSpoolInvalid("progress_file_invalid") from None
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != (os.geteuid() if owner_uid is None else owner_uid)
        or info.st_mode & (0o037 if group_readable else 0o077)
        or info.st_nlink != 1
        or info.st_size > MAX_JOURNAL_BYTES
    ):
        os.close(descriptor)
        raise ProgressSpoolInvalid("progress_file_invalid")
    return descriptor


def _line(record: dict[str, Any]) -> bytes:
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_RECORD_BYTES:
        raise ProgressSpoolInvalid("progress_record_limit")
    return encoded


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in items:
        if key in record:
            raise ProgressSpoolInvalid("progress_record_invalid")
        record[key] = value
    return record


def _read(descriptor: int) -> bytes:
    size = os.fstat(descriptor).st_size
    if size > MAX_JOURNAL_BYTES:
        raise ProgressSpoolInvalid("progress_size_limit")
    os.lseek(descriptor, 0, os.SEEK_SET)
    parts: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ProgressSpoolInvalid("progress_read_incomplete")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _decode(raw: bytes, identity: dict[str, str]) -> ProgressSnapshot:
    if not raw.endswith(b"\n"):
        raise ProgressSpoolInvalid("progress_record_invalid")
    lines = raw.splitlines(keepends=True)
    if not lines or len(lines) > MAX_PROGRESS + 1:
        raise ProgressSpoolInvalid("progress_size_limit")
    try:
        header = json.loads(lines[0], object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError):
        raise ProgressSpoolInvalid("progress_record_invalid") from None
    expected = {"schema_version": 1, "record_type": "progress_open", **identity}
    if header != expected or _line(header) != lines[0]:
        raise ProgressSpoolInvalid("progress_identity_invalid")
    previous = hashlib.sha256(lines[0]).hexdigest()
    records: list[ProgressRecord] = []
    for sequence, line in enumerate(lines[1:], start=1):
        if len(line) > MAX_RECORD_BYTES:
            raise ProgressSpoolInvalid("progress_record_limit")
        try:
            value = json.loads(line, object_pairs_hook=_pairs)
        except (UnicodeDecodeError, ValueError):
            raise ProgressSpoolInvalid("progress_record_invalid") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"sequence", "source", "kind", "text", "tool", "command", "previous_sha256"}
            or type(value["sequence"]) is not int
            or value["sequence"] != sequence
            or value["previous_sha256"] != previous
            or _line(value) != line
        ):
            raise ProgressSpoolInvalid("progress_record_invalid")
        try:
            progress = admit_progress(
                ProviderProgress(
                    value["kind"], value["text"], value["tool"], value["command"], value["source"]
                )
            )
        except (TypeError, ValueError):
            raise ProgressSpoolInvalid("progress_record_invalid") from None
        records.append(ProgressRecord(sequence, progress))
        previous = hashlib.sha256(line).hexdigest()
    return ProgressSnapshot(tuple(records), len(records))


class LiveProgressJournal:
    """One writer opened after an exclusive attempt claim; never reopened for replay."""

    def __init__(self, descriptor: int, directory: int, identity: dict[str, str], header: bytes) -> None:
        self._descriptor = descriptor
        self._directory = directory
        self._identity = identity
        self._sequence = 0
        self._bytes = len(header)
        self._previous = hashlib.sha256(header).hexdigest()
        self._lock = Lock()
        self._unknown = False

    @classmethod
    def create(
        cls,
        spool: Path,
        project_id: str,
        run_id: str,
        attempt_id: str,
        request_sha256: str,
        *,
        group_readable: bool = False,
    ) -> LiveProgressJournal:
        identity = _identity(project_id, run_id, attempt_id, request_sha256)
        directory = _claims(spool)
        descriptor = -1
        try:
            claim = _checked_file(
                directory, f"{attempt_id}.claim", os.O_RDONLY, group_readable=group_readable
            )
            try:
                if os.read(claim, 65) != attempt_id.encode("ascii"):
                    raise ProgressSpoolInvalid("progress_claim_invalid")
            finally:
                os.close(claim)
            header = _line({"schema_version": 1, "record_type": "progress_open", **identity})
            try:
                descriptor = os.open(
                    f"{attempt_id}.progress.jsonl",
                    os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o640 if group_readable else 0o600,
                    dir_fd=directory,
                )
                os.fchmod(descriptor, 0o640 if group_readable else 0o600)
            except OSError:
                raise ProgressSpoolInvalid("progress_exists_or_unavailable") from None
            if os.write(descriptor, header) != len(header):
                raise ProgressSpoolInvalid("progress_initialize_unknown")
            os.fsync(descriptor)
            os.fsync(directory)
            return cls(descriptor, directory, identity, header)
        except (OSError, ProgressSpoolInvalid) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory)
            if isinstance(exc, ProgressSpoolInvalid):
                raise
            raise ProgressSpoolInvalid("progress_initialize_unknown") from None

    def append(self, item: ProviderProgress) -> bool:
        item = admit_progress(item)
        with self._lock:
            if self._unknown or self._descriptor < 0:
                raise ProgressSpoolInvalid("progress_write_unknown")
            if self._sequence >= MAX_PROGRESS:
                return False
            record = {
                "sequence": self._sequence + 1,
                "source": item.source,
                "kind": item.kind,
                "text": item.text,
                "tool": item.tool,
                "command": item.command,
                "previous_sha256": self._previous,
            }
            line = _line(record)
            if self._bytes + len(line) > MAX_JOURNAL_BYTES:
                return False
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_EX)
                if os.fstat(self._descriptor).st_size != self._bytes:
                    raise OSError("progress file changed")
                offset = 0
                while offset < len(line):
                    written = os.write(self._descriptor, line[offset:])
                    if written <= 0:
                        raise OSError("progress append incomplete")
                    offset += written
                os.fsync(self._descriptor)
            except OSError:
                self._unknown = True
                raise ProgressSpoolInvalid("progress_write_unknown") from None
            finally:
                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            self._bytes += len(line)
            self._sequence += 1
            self._previous = hashlib.sha256(line).hexdigest()
            return True

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            os.close(self._directory)
            self._descriptor = -1

    def __enter__(self) -> LiveProgressJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def read_progress(
    spool: Path,
    project_id: str,
    run_id: str,
    attempt_id: str,
    request_sha256: str,
    *,
    owner_uid: int | None = None,
    group_readable: bool = False,
) -> ProgressSnapshot:
    """Read only a locked, identity-bound, fully validated journal prefix."""
    identity = _identity(project_id, run_id, attempt_id, request_sha256)
    directory = _claims(spool)
    try:
        try:
            os.stat(f"{attempt_id}.progress.jsonl", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            # Absence means the claimed runner has not started publishing yet.
            return ProgressSnapshot((), 0)
        descriptor = _checked_file(
            directory,
            f"{attempt_id}.progress.jsonl",
            os.O_RDONLY,
            owner_uid=owner_uid,
            group_readable=group_readable,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            raw = _read(descriptor)
            return _decode(raw, identity)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
