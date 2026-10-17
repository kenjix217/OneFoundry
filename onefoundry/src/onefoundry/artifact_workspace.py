"""Private, bounded local bytes for database-owned text artifacts.

This module has no database transaction or ownership claim. A caller must store
the returned path, digest, and size in its own project-scoped artifact record.
Unrecorded files are orphans and must never be treated as successful artifacts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from onefoundry.role_sensitive_text import contains_sensitive_text

MAX_ARTIFACT_BYTES = 65536
_KIND_MIME = {"text": "text/plain", "markdown": "text/markdown", "csv": "text/csv", "video": "video/webm"}
VIDEO_BYTE_CEILING = 67_108_864
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ArtifactWorkspaceError(Exception):
    """Base class for local workspace failure."""


class ArtifactInvalid(ArtifactWorkspaceError, ValueError):
    """Input or workspace configuration violates the storage contract."""


class ArtifactAlreadyExists(ArtifactWorkspaceError):
    """This artifact ID is already occupied; never overwrite it."""


class ArtifactMissing(ArtifactWorkspaceError):
    """The exact recorded artifact is absent."""


class ArtifactCorrupt(ArtifactWorkspaceError):
    """Stored bytes or file identity do not match the owner record."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    relative_path: str
    sha256: str
    byte_size: int


def _identity_path(project_id: UUID, run_id: UUID, artifact_id: UUID, kind: str = "text") -> str:
    if not all(isinstance(item, UUID) for item in (project_id, run_id, artifact_id)):
        raise ArtifactInvalid("artifact IDs must be UUIDs")
    extension = "webm" if kind == "video" else "txt"
    return f"{project_id}/{run_id}/{artifact_id}.{extension}"


def _validate_type(kind: str, mime_type: str) -> None:
    if not isinstance(kind, str) or not isinstance(mime_type, str) or _KIND_MIME.get(kind) != mime_type:
        raise ArtifactInvalid("artifact kind and MIME type are not allowed")


def _validate_content(content: bytes, kind: str, max_video_bytes: int = 0) -> None:
    if kind == "video":
        if type(max_video_bytes) is not int or not 1 <= max_video_bytes <= VIDEO_BYTE_CEILING:
            raise ArtifactInvalid("an explicit bounded video storage configuration is required")
        if not isinstance(content, bytes) or not 1 <= len(content) <= max_video_bytes:
            raise ArtifactInvalid("recorded video exceeds its configured byte bound or is empty")
        # Container identification is not playback proof. The operator verifies
        # the saved, hash-bound clip in the browser before QA may pass it.
        if not content.startswith(b"\x1a\x45\xdf\xa3") or b"webm" not in content[:256]:
            raise ArtifactInvalid("recorded video must be a WebM container")
        return
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_ARTIFACT_BYTES:
        raise ArtifactInvalid("artifact must contain 1 to 65536 bytes")
    try:
        decoded = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArtifactInvalid("artifact must be UTF-8 text") from exc
    if not decoded.strip() or any(
        (ord(char) < 32 and char not in "\r\n\t") or ord(char) in (127, 0xFEFF) for char in decoded
    ):
        raise ArtifactInvalid("artifact contains empty or invalid text")
    if contains_sensitive_text(decoded):
        raise ArtifactInvalid("artifact contains sensitive text")
    if kind == "csv":
        try:
            rows = csv.reader(io.StringIO(decoded, newline=""), strict=True)
            for row in rows:
                if any(cell.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) for cell in row):
                    raise ArtifactInvalid("CSV contains a formula cell")
        except csv.Error as exc:
            raise ArtifactInvalid("CSV is malformed") from exc


def _check_private_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ArtifactInvalid("artifact workspace directory must be private and owned by this process")


def _open_child(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError as exc:
        raise ArtifactMissing("artifact workspace directory missing") from exc
    except OSError as exc:
        raise ArtifactInvalid("artifact workspace directory unavailable") from exc
    try:
        _check_private_directory(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class ArtifactWorkspace:
    """Store and verify UTF-8 artifacts beneath an injected, existing root."""

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        if not path.is_absolute() or path == Path("/") or ".." in path.parts:
            raise ArtifactInvalid("artifact workspace root must be an absolute dedicated directory")
        self.root = path

    def _open_root(self) -> int:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        try:
            for part in self.root.parts[1:]:
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except OSError as exc:
                    raise ArtifactInvalid("artifact workspace root unavailable") from exc
                os.close(descriptor)
                descriptor = child
            _check_private_directory(descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def check_ready(self) -> None:
        """Require the configured private volume to be mounted and owned by the app."""
        descriptor = self._open_root()
        os.close(descriptor)

    def _open_run(self, project_id: UUID, run_id: UUID, *, create: bool) -> int:
        root = self._open_root()
        try:
            project = _open_child(root, str(project_id), create=create)
        finally:
            os.close(root)
        try:
            return _open_child(project, str(run_id), create=create)
        finally:
            os.close(project)

    def write(
        self,
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        *,
        kind: str,
        mime_type: str,
        content: bytes,
        max_video_bytes: int = 0,
    ) -> StoredArtifact:
        relative_path = _identity_path(project_id, run_id, artifact_id, kind)
        _validate_type(kind, mime_type)
        _validate_content(content, kind, max_video_bytes)
        run = self._open_run(project_id, run_id, create=True)
        temporary = f".{artifact_id}.{uuid4()}.tmp"
        final = f"{artifact_id}.webm" if kind == "video" else f"{artifact_id}.txt"
        created = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=run,
            )
            created = True
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, final, src_dir_fd=run, dst_dir_fd=run, follow_symlinks=False)
            except FileExistsError as exc:
                raise ArtifactAlreadyExists("artifact ID already exists") from exc
            os.unlink(temporary, dir_fd=run)
            created = False
            os.fsync(run)
        finally:
            if created:
                os.unlink(temporary, dir_fd=run)
            os.close(run)
        return StoredArtifact(relative_path, hashlib.sha256(content).hexdigest(), len(content))

    def read(
        self,
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        *,
        relative_path: str,
        expected_sha256: str,
        expected_byte_size: int,
        kind: str,
        mime_type: str,
        max_video_bytes: int = 0,
    ) -> bytes:
        exact_path = _identity_path(project_id, run_id, artifact_id, kind)
        _validate_type(kind, mime_type)
        maximum = max_video_bytes if kind == "video" else MAX_ARTIFACT_BYTES
        if kind == "video" and (type(maximum) is not int or not 1 <= maximum <= VIDEO_BYTE_CEILING):
            raise ArtifactInvalid("an explicit bounded video storage configuration is required")
        if (
            not isinstance(relative_path, str)
            or relative_path != exact_path
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None
            or type(expected_byte_size) is not int
            or not 1 <= expected_byte_size <= maximum
        ):
            raise ArtifactInvalid("artifact owner metadata is invalid")
        run = self._open_run(project_id, run_id, create=False)
        try:
            try:
                descriptor = os.open(
                    f"{artifact_id}.webm" if kind == "video" else f"{artifact_id}.txt",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=run,
                )
            except FileNotFoundError as exc:
                raise ArtifactMissing("artifact is missing") from exc
            except OSError as exc:
                raise ArtifactCorrupt("artifact file is unsafe") from exc
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size != expected_byte_size
                ):
                    raise ArtifactCorrupt("artifact file identity or size mismatch")
                chunks: list[bytes] = []
                remaining = maximum + 1
                while remaining:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                if (
                    len(content) != expected_byte_size
                    or hashlib.sha256(content).hexdigest() != expected_sha256
                ):
                    raise ArtifactCorrupt("artifact bytes do not match owner metadata")
                try:
                    _validate_content(content, kind, max_video_bytes)
                except ArtifactInvalid as exc:
                    raise ArtifactCorrupt("artifact content is invalid") from exc
                return content
            finally:
                os.close(descriptor)
        finally:
            os.close(run)
