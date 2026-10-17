"""Private original bytes for project-owned input files.

The database is the owner of file identity. This module never accepts a display
name as a path and never treats an unrecorded original as an uploaded file.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class InputFileInvalid(ValueError):
    """A file failed an exact storage or integrity check."""


class InputFileTooLarge(InputFileInvalid):
    """The streaming input exceeded its configured byte limit."""


class InputFileAlreadyExists(InputFileInvalid):
    """The exact input ID has already been installed."""


@dataclass(frozen=True, slots=True)
class StoredInputFile:
    relative_path: str
    sha256: str
    byte_size: int


class InputFileBytes:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if not self.root.is_absolute() or self.root == Path("/") or ".." in self.root.parts:
            raise InputFileInvalid("input root must be an absolute dedicated directory")
        descriptor = self._root_fd()
        os.close(descriptor)

    def _root_fd(self) -> int:
        descriptor = os.open("/", _DIR_FLAGS)
        try:
            for part in self.root.parts[1:]:
                next_descriptor = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise InputFileInvalid("input root must be private and owned by this process")
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            raise InputFileInvalid("input root symlink or directory unavailable") from exc
        except BaseException:
            os.close(descriptor)
            raise

    def _project_fd(self, project_id: UUID, *, create: bool) -> int:
        if not isinstance(project_id, UUID):
            raise InputFileInvalid("project ID is invalid")
        root = self._root_fd()
        try:
            if create:
                try:
                    os.mkdir(str(project_id), 0o700, dir_fd=root)
                except FileExistsError:
                    pass
            descriptor = os.open(str(project_id), _DIR_FLAGS, dir_fd=root)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                os.close(descriptor)
                raise InputFileInvalid("project input directory must be private")
            return descriptor
        except FileNotFoundError as exc:
            raise InputFileInvalid("stored input does not match its owner") from exc
        finally:
            os.close(root)

    @staticmethod
    def _validate(file_id: UUID, max_bytes: int) -> None:
        if not isinstance(file_id, UUID) or not 1 <= max_bytes <= 1_000_000_000:
            raise InputFileInvalid("file identity or size limit is invalid")

    def _start(self, project_id: UUID, file_id: UUID, max_bytes: int) -> tuple[int, int, str]:
        self._validate(file_id, max_bytes)
        project = self._project_fd(project_id, create=True)
        temporary = f".{file_id}.{uuid4()}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=project,
            )
            return project, descriptor, temporary
        except BaseException:
            os.close(project)
            raise

    def _complete(
        self,
        project_id: UUID,
        file_id: UUID,
        project: int,
        temporary: str,
        digest: hashlib._Hash,
        size: int,
        max_bytes: int,
    ) -> StoredInputFile:
        if not 1 <= size <= max_bytes:
            raise InputFileInvalid("input file is empty or exceeds the configured limit")
        final = str(file_id)
        try:
            os.link(temporary, final, src_dir_fd=project, dst_dir_fd=project, follow_symlinks=False)
        except FileExistsError as exc:
            raise InputFileAlreadyExists("input file ID already exists") from exc
        os.unlink(temporary, dir_fd=project)
        os.fsync(project)
        return StoredInputFile(f"{project_id}/{file_id}", digest.hexdigest(), size)

    def _cleanup(self, project_id: UUID, project: int, temporary: str) -> None:
        try:
            os.unlink(temporary, dir_fd=project)
        except FileNotFoundError:
            pass
        os.close(project)
        root = self._root_fd()
        try:
            try:
                os.rmdir(str(project_id), dir_fd=root)
            except OSError:
                pass
        finally:
            os.close(root)

    def write(
        self, project_id: UUID, file_id: UUID, chunks: Iterable[bytes], *, max_bytes: int
    ) -> StoredInputFile:
        project, descriptor, temporary = self._start(project_id, file_id, max_bytes)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise InputFileInvalid("input stream contains non-bytes")
                    size += len(chunk)
                    if size > max_bytes:
                        raise InputFileTooLarge("input file exceeds the configured limit")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            return self._complete(project_id, file_id, project, temporary, digest, size, max_bytes)
        finally:
            self._cleanup(project_id, project, temporary)

    async def write_async(
        self, project_id: UUID, file_id: UUID, chunks: AsyncIterable[bytes], *, max_bytes: int
    ) -> StoredInputFile:
        project, descriptor, temporary = self._start(project_id, file_id, max_bytes)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise InputFileTooLarge("input file exceeds the configured limit")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            return self._complete(project_id, file_id, project, temporary, digest, size, max_bytes)
        finally:
            self._cleanup(project_id, project, temporary)

    def read(self, project_id: UUID, file_id: UUID, sha256: str, byte_size: int) -> bytes:
        if not isinstance(file_id, UUID) or len(sha256) != 64 or byte_size < 1:
            raise InputFileInvalid("stored input identity is invalid")
        project = self._project_fd(project_id, create=False)
        try:
            try:
                descriptor = os.open(str(file_id), _FILE_FLAGS, dir_fd=project)
            except OSError as exc:
                raise InputFileInvalid("stored input does not match its owner") from exc
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != byte_size:
                    raise InputFileInvalid("stored input does not match its owner")
                content = source.read(byte_size + 1)
            if len(content) != byte_size or hashlib.sha256(content).hexdigest() != sha256:
                raise InputFileInvalid("stored input does not match its owner")
            return content
        finally:
            os.close(project)

    def remove(self, project_id: UUID, file_id: UUID) -> None:
        project = self._project_fd(project_id, create=False)
        try:
            os.unlink(str(file_id), dir_fd=project)
            os.fsync(project)
        finally:
            os.close(project)
