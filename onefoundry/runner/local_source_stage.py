"""Bounded, offline staging of one pinned local Git tree for a trusted runner."""

from __future__ import annotations

import hashlib
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

SHA = re.compile(r"[0-9a-f]{40}\Z")
GIT = "/usr/bin/git"


class StageError(ValueError):
    """A source cannot be safely staged; the code is safe for receipts."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LocalSource:
    source_id: UUID
    root_id: str
    relative_path: str
    branch: str
    revision: str

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> LocalSource:
        if set(value) != {"source_id", "root_id", "relative_path", "branch", "revision"}:
            raise StageError("invalid_source_fields")
        try:
            source_id = UUID(str(value["source_id"]))
        except (ValueError, TypeError):
            raise StageError("invalid_source_id") from None
        if str(source_id) != value["source_id"]:
            raise StageError("invalid_source_id")
        root_id = value["root_id"]
        relative_path = value["relative_path"]
        branch = value["branch"]
        revision = value["revision"]
        if root_id != "workspace":
            raise StageError("invalid_root_id")
        if not isinstance(relative_path, str) or not _valid_relative_path(relative_path):
            raise StageError("invalid_relative_path")
        if not isinstance(branch, str) or not 1 <= len(branch) <= 200 or "\x00" in branch:
            raise StageError("invalid_branch")
        if not isinstance(revision, str) or SHA.fullmatch(revision) is None:
            raise StageError("invalid_revision")
        return cls(source_id, root_id, relative_path, branch, revision)


@dataclass(frozen=True)
class StageLimits:
    max_files: int
    max_bytes: int
    max_tree_bytes: int
    wall_seconds: float

    def validate(self) -> None:
        if (
            type(self.max_files) is not int
            or self.max_files < 1
            or type(self.max_bytes) is not int
            or self.max_bytes < 1
            or type(self.max_tree_bytes) is not int
            or self.max_tree_bytes < 1
            or not isinstance(self.wall_seconds, (int, float))
            or isinstance(self.wall_seconds, bool)
            or not 0 < self.wall_seconds < float("inf")
        ):
            raise StageError("invalid_stage_limits")


@dataclass(frozen=True)
class StageResult:
    source_id: UUID
    source_revision: str
    source_tree: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class _TreeFile:
    path: str
    object_id: str
    size: int
    executable: bool


def _valid_relative_path(value: str) -> bool:
    return value == "." or (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and all(ord(char) >= 32 for char in value)
    )


def _regular_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


@dataclass(frozen=True)
class _AdmittedRepo:
    repo_fd: int
    git_fd: int
    objects_fd: int
    identities: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]

    @property
    def work_tree(self) -> str:
        return f"/proc/self/fd/{self.repo_fd}"

    @property
    def git_dir(self) -> str:
        return f"/proc/self/fd/{self.git_fd}"

    @property
    def object_dir(self) -> str:
        return f"/proc/self/fd/{self.objects_fd}"

    def check_identity(self) -> None:
        try:
            actual = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in self.fds)
        except OSError:
            raise StageError("source_repo_invalid") from None
        if actual != self.identities:
            raise StageError("source_repo_invalid")

    @property
    def fds(self) -> tuple[int, int, int]:
        return (self.repo_fd, self.git_fd, self.objects_fd)


def _open_directory(name: str, parent_fd: int, error: str, stack: ExitStack) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        raise StageError(error) from None
    stack.callback(os.close, fd)
    return fd


def _reject_alternates(objects_fd: int) -> None:
    try:
        info_fd = os.open("info", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=objects_fd)
    except FileNotFoundError:
        return
    except OSError:
        raise StageError("source_repo_invalid") from None
    try:
        try:
            os.stat("alternates", dir_fd=info_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise StageError("source_repo_uses_alternates")
    finally:
        os.close(info_fd)


def _reject_common_dir(git_fd: int) -> None:
    try:
        os.stat("commondir", dir_fd=git_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise StageError("source_repo_invalid") from None
    raise StageError("source_repo_invalid")


@contextmanager
def _admit_repo(root: Path, relative_path: str) -> Iterator[_AdmittedRepo]:
    if not root.is_absolute():
        raise StageError("source_root_unavailable")
    with ExitStack() as stack:
        try:
            slash_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise StageError("source_root_unavailable") from None
        stack.callback(os.close, slash_fd)
        root_fd = slash_fd
        for part in root.parts[1:]:
            root_fd = _open_directory(part, root_fd, "source_root_unavailable", stack)
        repo_fd = root_fd
        for part in () if relative_path == "." else relative_path.split("/"):
            repo_fd = _open_directory(part, repo_fd, "source_path_unavailable", stack)
        git_fd = _open_directory(".git", repo_fd, "source_repo_invalid", stack)
        objects_fd = _open_directory("objects", git_fd, "source_repo_invalid", stack)
        _reject_common_dir(git_fd)
        _reject_alternates(objects_fd)
        fds = (repo_fd, git_fd, objects_fd)
        identities = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in fds)
        yield _AdmittedRepo(repo_fd, git_fd, objects_fd, (identities[0], identities[1], identities[2]))


def _git_env(repo: _AdmittedRepo) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OBJECT_DIRECTORY": repo.object_dir,
    }


def _git_read(repo: _AdmittedRepo, arguments: tuple[str, ...], output_limit: int, deadline: float) -> bytes:
    repo.check_identity()
    _reject_common_dir(repo.git_fd)
    _reject_alternates(repo.objects_fd)
    command = (
        GIT,
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repo.work_tree}",
        f"--git-dir={repo.git_dir}",
        f"--work-tree={repo.work_tree}",
        *arguments,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed Git executable and admitted repo
            command,
            env=_git_env(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=repo.fds,
        )
        assert process.stdout is not None
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StageError("source_stage_timeout")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise StageError("source_stage_timeout")
            chunk = os.read(process.stdout.fileno(), min(65536, output_limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > output_limit:
                raise StageError("source_output_limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode != 0:
            raise StageError("source_git_failed")
        repo.check_identity()
        _reject_common_dir(repo.git_fd)
        _reject_alternates(repo.objects_fd)
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired):
        raise StageError("source_git_unavailable") from None
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
        raise StageError("source_git_output_invalid") from None


def _verify_head(repo: _AdmittedRepo, source: LocalSource, deadline: float) -> str:
    top = _git_text(repo, ("rev-parse", "--show-toplevel"), deadline)
    try:
        top_info = os.stat(top) if Path(top).is_absolute() else None
    except OSError:
        top_info = None
    if (
        top_info is None
        or (top_info.st_dev, top_info.st_ino) != repo.identities[0]
        or _git_text(repo, ("rev-parse", "--is-bare-repository"), deadline) != "false"
    ):
        raise StageError("source_repo_invalid")
    head = _git_text(repo, ("rev-parse", "--verify", "HEAD^{commit}"), deadline)
    if head != source.revision:
        raise StageError("source_revision_drift")
    branch = _git_text(repo, ("rev-parse", "--abbrev-ref", "HEAD"), deadline)
    if branch != source.branch:
        raise StageError("source_branch_drift")
    tree = _git_text(repo, ("rev-parse", "--verify", "HEAD^{tree}"), deadline)
    if SHA.fullmatch(tree) is None:
        raise StageError("source_tree_invalid")
    return tree


def verify_local_source_pin(
    source_value: Mapping[str, object], root: Path, expected_tree: str, deadline: float
) -> None:
    """Reopen the admitted path without following links and verify its saved pin."""
    source = LocalSource.parse(source_value)
    with _admit_repo(root, source.relative_path) as repo:
        if _verify_head(repo, source, deadline) != expected_tree:
            raise StageError("source_revision_drift")


def _parse_tree(raw: bytes, limits: StageLimits) -> tuple[_TreeFile, ...]:
    if raw and not raw.endswith(b"\x00"):
        raise StageError("source_tree_invalid")
    entries: list[_TreeFile] = []
    total = 0
    paths: set[str] = set()
    directories: set[str] = set()
    for item in raw.split(b"\x00")[:-1]:
        try:
            metadata, encoded_path = item.split(b"\t", 1)
            mode, kind, object_id, encoded_size = metadata.decode("ascii").split()
            path = encoded_path.decode("utf-8")
            size = int(encoded_size)
        except (ValueError, UnicodeDecodeError):
            raise StageError("source_tree_invalid") from None
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or SHA.fullmatch(object_id) is None
            or not _valid_relative_path(path)
            or path == "."
            or any(part.casefold() == ".git" for part in path.split("/"))
            or path in paths
            or path in directories
            or size < 0
        ):
            raise StageError("source_tree_unsupported")
        parts = path.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if parents & paths:
            raise StageError("source_tree_unsupported")
        directories.update(parents)
        paths.add(path)
        total += size
        if len(paths) > limits.max_files or total > limits.max_bytes:
            raise StageError("source_tree_limit")
        entries.append(_TreeFile(path, object_id, size, mode == "100755"))
    return tuple(entries)


def _write_blob(repo: _AdmittedRepo, destination: Path, item: _TreeFile, deadline: float) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = _git_read(repo, ("cat-file", "blob", item.object_id), item.size, deadline)
    digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()  # noqa: S324 - Git SHA-1 object identity
    if len(data) != item.size or digest != item.object_id:
        raise StageError("source_blob_mismatch")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    if item.executable:
        destination.chmod(0o700)


def stage_local_source(
    source_value: Mapping[str, object], root: Path, destination: Path, limits: StageLimits
) -> StageResult:
    """Create a fresh plain checkout from the saved commit; remove partial output on failure."""
    source = LocalSource.parse(source_value)
    limits.validate()
    deadline = time.monotonic() + limits.wall_seconds
    with _admit_repo(root, source.relative_path) as repo:
        repo_path = root.joinpath(*(() if source.relative_path == "." else source.relative_path.split("/")))
        if (
            not destination.is_absolute()
            or not _regular_dir(destination.parent)
            or destination.parent.resolve() != destination.parent
            or destination.exists()
            or destination.is_symlink()
            or destination == repo_path
            or repo_path in destination.parents
        ):
            raise StageError("stage_destination_invalid")
        tree = _verify_head(repo, source, deadline)
        raw = _git_read(
            repo, ("ls-tree", "-rz", "-l", "--full-tree", source.revision), limits.max_tree_bytes, deadline
        )
        files = _parse_tree(raw, limits)
        destination.mkdir(mode=0o700)
        try:
            for item in files:
                _write_blob(repo, destination / item.path, item, deadline)
            if _verify_head(repo, source, deadline) != tree:
                raise StageError("source_revision_drift")
        except Exception:
            shutil.rmtree(destination)
            raise
        return StageResult(
            source.source_id, source.revision, tree, len(files), sum(item.size for item in files)
        )
