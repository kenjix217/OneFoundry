"""Trusted, bounded read of an exact saved local or GitHub repository file."""

from __future__ import annotations

import hashlib
import os
import re
import select
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text

from onefoundry.role_sensitive_text import contains_sensitive_text

if TYPE_CHECKING:
    from sqlalchemy import Connection

    from onefoundry.role_tool_admission import RunContext
    from onefoundry.settings import Settings

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PATH = re.compile(r"[A-Za-z0-9._/-]{1,1024}\Z")
PRIVATE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets",
}
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
REPOSITORY_FILE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "format": "uuid"},
        "path": {"type": "string", "minLength": 1, "maxLength": 1024},
    },
    "required": ["source_id", "path"],
    "additionalProperties": False,
}


class RepositoryReadError(ValueError):
    """The saved source cannot be read safely."""

    def __init__(self, message: str, *, code: str = "source_unavailable") -> None:
        super().__init__(message)
        self.code = code


def _valid_path(value: str) -> bool:
    if PATH.fullmatch(value) is None or value.startswith("/"):
        return False
    return all(
        part not in {"", ".", ".."}
        and part.casefold() != ".git"
        and part.casefold() not in PRIVATE_NAMES
        and not part.casefold().startswith(".env.")
        and not part.casefold().endswith(PRIVATE_SUFFIXES)
        for part in value.split("/")
    )


def _valid_repo_path(value: str) -> bool:
    return value == "." or (
        0 < len(value) <= 500
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and all(32 <= ord(char) < 127 or ord(char) >= 160 for char in value)
    )


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    path: str

    @field_validator("source_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            if str(UUID(value)) == value:
                return value
        except ValueError:
            pass
        raise ValueError("invalid source_id")

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if not _valid_path(value):
            raise ValueError("invalid repository file path")
        return value


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    found: bool
    revision: str | None = None
    path: str | None = None
    sha256: str | None = None
    byte_size: int | None = None
    text: str | None = None
    truncated: bool | None = None


def parse_repository_file(arguments: object) -> dict[str, Any]:
    parsed = _Arguments.model_validate(arguments)
    return {"source_id": parsed.source_id, "path": parsed.path}


@dataclass(frozen=True)
class _AdmittedRepo:
    repo_fd: int
    git_fd: int
    objects_fd: int
    identities: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]

    @property
    def fds(self) -> tuple[int, int, int]:
        return self.repo_fd, self.git_fd, self.objects_fd

    def check(self) -> None:
        try:
            actual = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in self.fds)
        except OSError:
            raise RepositoryReadError("repository unavailable") from None
        if actual != self.identities:
            raise RepositoryReadError("repository changed during read")
        try:
            os.stat("commondir", dir_fd=self.git_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RepositoryReadError("repository uses an external Git directory")
        try:
            info_fd = os.open("info", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.objects_fd)
        except FileNotFoundError:
            return
        except OSError:
            raise RepositoryReadError("repository unavailable") from None
        try:
            try:
                os.stat("alternates", dir_fd=info_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise RepositoryReadError("repository uses external Git objects")
        finally:
            os.close(info_fd)


def _open_dir(parent: int, name: str, stack: ExitStack) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        raise RepositoryReadError("repository path unavailable") from None
    stack.callback(os.close, fd)
    return fd


@contextmanager
def _admit_repo(root: Path, relative_path: str) -> Iterator[_AdmittedRepo]:
    if not root.is_absolute() or not _valid_repo_path(relative_path):
        raise RepositoryReadError("repository path invalid")
    with ExitStack() as stack:
        try:
            slash_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise RepositoryReadError("repository root unavailable") from None
        stack.callback(os.close, slash_fd)
        root_fd = slash_fd
        for part in root.parts[1:]:
            root_fd = _open_dir(root_fd, part, stack)
        repo_fd = root_fd
        for part in () if relative_path == "." else relative_path.split("/"):
            repo_fd = _open_dir(repo_fd, part, stack)
        git_fd = _open_dir(repo_fd, ".git", stack)
        objects_fd = _open_dir(git_fd, "objects", stack)
        fds = repo_fd, git_fd, objects_fd
        identities = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in fds)
        repo = _AdmittedRepo(repo_fd, git_fd, objects_fd, (identities[0], identities[1], identities[2]))
        repo.check()
        yield repo


def _git_read(repo: _AdmittedRepo, arguments: tuple[str, ...], limit: int, deadline: float) -> bytes:
    repo.check()
    command = (
        "/usr/bin/git",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory=/proc/self/fd/{repo.repo_fd}",
        f"--git-dir=/proc/self/fd/{repo.git_fd}",
        f"--work-tree=/proc/self/fd/{repo.repo_fd}",
        *arguments,
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_OBJECT_DIRECTORY": f"/proc/self/fd/{repo.objects_fd}",
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed Git executable and admitted descriptors
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=repo.fds,
        )
        assert process.stdout is not None
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise RepositoryReadError("repository read timed out", code="source_probe_timed_out")
            chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise RepositoryReadError("repository output exceeded limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode != 0:
            raise RepositoryReadError("repository Git read failed")
        repo.check()
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired):
        raise RepositoryReadError(
            "repository Git read unavailable", code="source_dependency_unavailable"
        ) from None
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if process.stdout is not None:
                process.stdout.close()


def _git_text(repo: _AdmittedRepo, arguments: tuple[str, ...], deadline: float) -> str:
    try:
        return _git_read(repo, arguments, 4096, deadline).decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RepositoryReadError("repository Git metadata invalid") from None


def _head(repo: _AdmittedRepo, deadline: float) -> tuple[str, str]:
    if _git_text(repo, ("rev-parse", "--is-bare-repository"), deadline) != "false":
        raise RepositoryReadError("repository is not a regular checkout")
    return (
        _git_text(repo, ("rev-parse", "--abbrev-ref", "HEAD"), deadline),
        _git_text(repo, ("rev-parse", "--verify", "HEAD^{commit}"), deadline),
    )


def _blob(repo: _AdmittedRepo, revision: str, path: str, limit: int, deadline: float) -> bytes:
    raw = _git_read(
        repo, ("ls-tree", "-z", "-l", "--full-tree", revision, "--", path), len(path) + 160, deadline
    )
    try:
        metadata, found_path = raw.removesuffix(b"\0").split(b"\t", 1)
        mode, kind, object_id, size_text = metadata.decode("ascii").split()
        size = int(size_text)
    except (ValueError, UnicodeDecodeError):
        raise RepositoryReadError("repository file unavailable") from None
    if (
        not raw.endswith(b"\0")
        or found_path != path.encode("utf-8")
        or mode not in {"100644", "100755"}
        or kind != "blob"
        or SHA40.fullmatch(object_id) is None
        or not 0 <= size <= limit
    ):
        raise RepositoryReadError("repository file unsupported or too large")
    data = _git_read(repo, ("cat-file", "blob", object_id), size, deadline)
    actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()  # noqa: S324 - Git object ID
    if len(data) != size or actual != object_id:
        raise RepositoryReadError("repository blob mismatch")
    return data


def _source(connection: Connection, context: RunContext, source_id: str) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                "SELECT id,project_id,kind,root_id,relative_path,branch,revision,created_at "
                "FROM project_sources WHERE project_id=:project_id AND id=:source_id"
            ),
            {"project_id": str(context.project_id), "source_id": source_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def inspect_source(source: dict[str, Any], settings: Settings, deadline: float) -> str | None:
    """Probe the same bounded Git owner as reads, without reading file content."""
    if (
        source.get("kind") != "local"
        or source.get("root_id") != "workspace"
        or not isinstance(source.get("relative_path"), str)
        or not isinstance(source.get("branch"), str)
        or not isinstance(source.get("revision"), str)
        or SHA40.fullmatch(source["revision"]) is None
    ):
        return "source_unavailable"
    try:
        with _admit_repo(Path(settings.local_source_root), source["relative_path"]) as repo:
            if _head(repo, deadline) != (source["branch"], source["revision"]):
                return "source_revision_drifted"
    except RepositoryReadError as exc:
        return exc.code
    return None


def repository_file(
    connection: Connection, context: RunContext, arguments: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    source_id = arguments["source_id"]
    path = arguments["path"]
    source = _source(connection, context, source_id)
    if source is None:
        return {"source_id": source_id, "found": False}
    if source["kind"] == "github":
        from onefoundry.source_store import SourceStore

        data = SourceStore(connection.engine, "repository-reader", settings).read_github_file(
            context.project_id,
            UUID(source_id),
            source["revision"],
            path,
            limit=settings.live_text_max_output_bytes // 2,
            deadline=time.monotonic() + settings.github_timeout_seconds,
        )
    else:
        data = _local_file(source, path, settings)
    return _file_result(connection, context, source_id, path, source, data)


def repository_document(
    connection: Connection, context: RunContext, arguments: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """Screen the complete bounded blob before selecting a page."""
    source_id, path = arguments["source_id"], arguments["path"]
    source = _source(connection, context, source_id)
    if source is None:
        return {"source_id": source_id, "found": False}
    if source["kind"] == "github":
        from onefoundry.source_store import SourceStore

        data = SourceStore(connection.engine, "repository-reader", settings).read_github_file(
            context.project_id,
            UUID(source_id),
            source["revision"],
            path,
            limit=settings.input_file_max_text_bytes,
            deadline=time.monotonic() + settings.github_timeout_seconds,
        )
    else:
        data = _local_file(source, path, settings, limit=settings.input_file_max_text_bytes)
    return _file_result(connection, context, source_id, path, source, data)


def _local_file(source: dict[str, Any], path: str, settings: Settings, *, limit: int | None = None) -> bytes:
    if (
        source["kind"] != "local"
        or source["root_id"] != "workspace"
        or not isinstance(source["relative_path"], str)
        or not isinstance(source["branch"], str)
        or not isinstance(source["revision"], str)
        or SHA40.fullmatch(source["revision"]) is None
        or not settings.local_source_root
    ):
        raise RepositoryReadError("saved local source unavailable")
    deadline = time.monotonic() + settings.local_source_git_timeout_seconds
    with _admit_repo(Path(settings.local_source_root), source["relative_path"]) as repo:
        if _head(repo, deadline) != (source["branch"], source["revision"]):
            raise RepositoryReadError("saved source branch or revision drifted")
        data = _blob(
            repo,
            source["revision"],
            path,
            settings.live_text_max_output_bytes // 2 if limit is None else limit,
            deadline,
        )
        if _head(repo, deadline) != (source["branch"], source["revision"]):
            raise RepositoryReadError("saved source branch or revision drifted")
    return data


def _file_result(
    connection: Connection,
    context: RunContext,
    source_id: str,
    path: str,
    source: dict[str, Any],
    data: bytes,
) -> dict[str, Any]:
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RepositoryReadError("repository file is not UTF-8 text") from None
    if "\x00" in content or any(ord(char) < 32 and char not in "\t\n\r" for char in content):
        raise RepositoryReadError("repository file is not plain text")
    if contains_sensitive_text(content):
        raise RepositoryReadError("repository file appears to contain a secret")
    with connection.engine.connect() as fresh:
        if _source(fresh, context, source_id) != source:
            raise RepositoryReadError("saved source changed during read")
    result = {
        "source_id": source_id,
        "found": True,
        "revision": source["revision"],
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
        "text": content,
        "truncated": False,
    }
    validate_repository_file(result)
    return result


def validate_repository_file(result: dict[str, Any]) -> None:
    parsed = _Result.model_validate(result)
    _Arguments.canonical_uuid(parsed.source_id)
    if not parsed.found:
        if set(result) != {"source_id", "found"}:
            raise ValueError("missing source result contains extra fields")
        return
    if set(result) != {"source_id", "found", "revision", "path", "sha256", "byte_size", "text", "truncated"}:
        raise ValueError("repository file result fields invalid")
    if (
        parsed.revision is None
        or SHA40.fullmatch(parsed.revision) is None
        or parsed.path is None
        or not _valid_path(parsed.path)
        or parsed.sha256 is None
        or SHA256.fullmatch(parsed.sha256) is None
        or parsed.text is None
        or parsed.byte_size is None
        or parsed.byte_size != len(parsed.text.encode("utf-8"))
        or parsed.truncated is not False
        or hashlib.sha256(parsed.text.encode("utf-8")).hexdigest() != parsed.sha256
        or contains_sensitive_text(parsed.text)
    ):
        raise ValueError("repository file result invalid")
