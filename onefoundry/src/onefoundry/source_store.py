"""Project-scoped repository source persistence and local source verification."""

import os
import re
import select
import signal
import stat
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings
from onefoundry.source_github import list_repositories, resolve_revision
from onefoundry.source_schemas import GitHubAccountCreate, GitHubSourceCreate, LocalSourceCreate

SHA = re.compile(r"^[0-9a-f]{40}$")
SOURCE_COLUMNS = (
    "id,project_id,kind,display_name,root_id,relative_path,account_id,owner,repo,branch,revision,created_at"
)
GIT = ("/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


def _local_root(settings: Settings) -> Path | None:
    if not settings.local_source_root:
        return None
    root = Path(settings.local_source_root)
    try:
        info = root.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or root.resolve() != root:
        return None
    return root


def _admitted_local_path(root: Path, relative_path: str) -> Path:
    target = root
    try:
        if relative_path != ".":
            for part in relative_path.split("/"):
                if part in {"", ".", ".."}:
                    raise ValueError("invalid repository path component")
                target = target / part
                if not stat.S_ISDIR(target.lstat().st_mode):
                    raise ValueError("repository path contains a symlink or file")
        if target.resolve(strict=True) != target:
            raise ValueError("repository path contains a symlink")
        target.relative_to(root)
    except (OSError, ValueError):
        raise Conflict("repository path is outside the configured root or unavailable") from None
    if target.name == "*":
        raise Conflict("selected repository must be a regular directory")
    return target


def _git_command(path: Path, root: Path, *arguments: str) -> list[str]:
    # Git's safe.directory supports '*' and trailing '/*'. Admit and recheck
    # this exact canonical repository path before granting trust to one command.
    try:
        relative_path = str(path.relative_to(root))
    except ValueError:
        raise Conflict("repository path is outside the configured root") from None
    admitted = _admitted_local_path(root, relative_path)
    if admitted != path:
        raise Conflict("repository path changed during Git verification")
    return [*GIT, "-c", f"safe.directory={path}", "-C", str(path), *arguments]


def _git(path: Path, root: Path, timeout_seconds: float, *arguments: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed git executable and no shell
            _git_command(path, root, *arguments),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
            },
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Unavailable("local Git verification unavailable") from None
    if result.returncode != 0:
        raise Conflict("selected directory is not an accessible Git repository")
    return result.stdout.strip()


def _git_timeout(settings: Settings, deadline: float | None) -> float:
    if deadline is None:
        return float(settings.local_source_git_timeout_seconds)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Unavailable("local repository scan deadline exceeded")
    return min(float(settings.local_source_git_timeout_seconds), remaining)


def verify_local(settings: Settings, relative_path: str, deadline: float | None = None) -> tuple[str, str]:
    root = _local_root(settings)
    if root is None:
        raise Unavailable("no dedicated local repository root is configured")
    resolved = _admitted_local_path(root, relative_path)
    top = _git(resolved, root, _git_timeout(settings, deadline), "rev-parse", "--show-toplevel")
    if (
        Path(top).resolve() != resolved
        or _git(resolved, root, _git_timeout(settings, deadline), "rev-parse", "--is-bare-repository")
        != "false"
    ):
        raise Conflict("selected directory must be a regular Git repository root")
    revision = _git(resolved, root, _git_timeout(settings, deadline), "rev-parse", "HEAD")
    if not SHA.fullmatch(revision):
        raise Conflict("selected repository has no valid HEAD revision")
    branch = (
        _git(resolved, root, _git_timeout(settings, deadline), "symbolic-ref", "--quiet", "--short", "HEAD")
        if _has_branch(resolved, root, _git_timeout(settings, deadline))
        else "HEAD"
    )
    if not branch or len(branch) > 200:
        raise Conflict("selected repository has no valid branch")
    return branch, revision


def _has_branch(path: Path, root: Path, timeout_seconds: float) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - fixed git executable and no shell
            _git_command(path, root, "symbolic-ref", "--quiet", "HEAD"),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
            },
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Unavailable("local Git verification unavailable") from None
    return result.returncode == 0


def _git_dirty(path: Path, root: Path, timeout_seconds: float) -> bool:
    """Read one status byte, then stop Git; never buffer a large file listing."""
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed git executable and no shell
            _git_command(path, root, "status", "--porcelain", "--untracked-files=normal"),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], timeout_seconds)
        if not readable:
            raise Unavailable("local Git status deadline exceeded")
        dirty = bool(os.read(process.stdout.fileno(), 1))
        if dirty:
            return True
        process.wait(timeout=timeout_seconds)
        if process.returncode != 0:
            raise Conflict("local Git status failed")
        return False
    except (OSError, subprocess.TimeoutExpired):
        raise Unavailable("local Git status unavailable") from None
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


class SourceStore:
    def __init__(self, engine: Engine, actor_id: str, settings: Settings) -> None:
        self.engine = engine
        self.actor_id = actor_id
        self.settings = settings
        self.secrets = ModelSecretStore(str(Path(settings.model_secret_root) / "github"))

    def prepare_secrets(self) -> None:
        ModelSecretStore(self.settings.model_secret_root).check_ready()
        try:
            self.secrets.root.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise Unavailable("GitHub credential store unavailable") from None
        self.secrets.check_ready()

    def reconcile_orphan_secrets(self) -> None:
        with self.engine.connect() as connection:
            active_ids: set[UUID] = set(
                connection.execute(text("SELECT id FROM github_accounts")).scalars().all()
            )
        self.secrets.remove_orphans(active_ids)

    def roots(self) -> list[dict[str, Any]]:
        if not self.settings.local_source_root:
            return []
        return [
            {
                "id": "workspace",
                "label": "Configured local repository root",
                "available": _local_root(self.settings) is not None,
            }
        ]

    def discover_local(self) -> dict[str, Any]:
        root = _local_root(self.settings)
        if root is None:
            return {"available": False, "truncated": False, "repositories": []}
        deadline = time.monotonic() + self.settings.local_source_scan_seconds
        candidates: list[tuple[str, Path]] = [(".", root)]
        truncated = False
        try:
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= self.settings.local_source_scan_max_entries:
                        truncated = True
                        break
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        candidates.append((entry.name, Path(entry.path)))
        except OSError:
            return {"available": False, "truncated": False, "repositories": []}
        repositories: list[dict[str, Any]] = []
        for relative_path, path in candidates:
            if time.monotonic() >= deadline:
                truncated = True
                break
            try:
                branch, revision = verify_local(self.settings, relative_path, deadline)
                admitted = _admitted_local_path(root, relative_path)
                dirty = _git_dirty(admitted, root, _git_timeout(self.settings, deadline))
            except (Conflict, Unavailable):
                continue
            repositories.append(
                {
                    "relative_path": relative_path,
                    "display_name": path.name,
                    "branch": branch,
                    "revision": revision,
                    "dirty": dirty,
                }
            )
        return {"available": True, "truncated": truncated, "repositories": repositories}

    def _active_project(self, project_id: UUID) -> None:
        with self.engine.connect() as connection:
            status = connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id})
        if status is None:
            raise NotFound("project not found")
        if status != "active":
            raise Conflict("restore this project before changing sources")

    def list_sources(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            status = connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id})
            if status is None:
                raise NotFound("project not found")
            rows = (
                connection.execute(
                    text(
                        f"SELECT {SOURCE_COLUMNS} FROM project_sources WHERE project_id=:project_id "  # noqa: S608
                        "ORDER BY created_at,id"
                    ),
                    {"project_id": project_id},
                )
                .mappings()
                .all()
            )
        output = []
        for row in rows:
            item = dict(row)
            item["availability"] = (
                "verified_at_selection"
                if item["kind"] == "github" or self._local_still_present(item)
                else "unavailable"
            )
            output.append(item)
        return output

    def _local_still_present(self, row: dict[str, Any]) -> bool:
        root = _local_root(self.settings)
        if root is None or row["root_id"] != "workspace":
            return False
        try:
            _admitted_local_path(root, row["relative_path"])
        except Conflict:
            return False
        return True

    def add_local(
        self, project_id: UUID, data: LocalSourceCreate, *, connection: Connection | None = None
    ) -> dict[str, Any]:
        self._active_project(project_id)
        branch, revision = verify_local(self.settings, data.relative_path)
        return self._insert_source(
            project_id,
            {
                "kind": "local",
                "display_name": (
                    Path(self.settings.local_source_root).name
                    if data.relative_path == "."
                    else Path(data.relative_path).name
                ),
                "root_id": data.root_id,
                "relative_path": data.relative_path,
                "account_id": None,
                "owner": None,
                "repo": None,
                "branch": branch,
                "revision": revision,
            },
            connection=connection,
        )

    def list_accounts(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            if connection.scalar(text("SELECT 1 FROM projects WHERE id=:id"), {"id": project_id}) is None:
                raise NotFound("project not found")
            rows = (
                connection.execute(
                    text(
                        "SELECT id,project_id,label FROM github_accounts WHERE project_id=:project_id "
                        "ORDER BY created_at,id"
                    ),
                    {"project_id": project_id},
                )
                .mappings()
                .all()
            )
        return [dict(row, credential_present=self.secrets.present(row["id"])) for row in rows]

    def add_account(self, project_id: UUID, data: GitHubAccountCreate) -> dict[str, Any]:
        self._active_project(project_id)
        token = data.token.get_secret_value()
        if not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("invalid GitHub credential")
        account_id = uuid4()
        self.secrets.write(account_id, token)
        try:
            with self.engine.begin() as connection:
                status = connection.scalar(
                    text("SELECT status FROM projects WHERE id=:id FOR SHARE"), {"id": project_id}
                )
                if status is None:
                    raise NotFound("project not found")
                if status != "active":
                    raise Conflict("restore this project before changing sources")
                row = (
                    connection.execute(
                        text(
                            "INSERT INTO github_accounts (id,project_id,actor_id,label) "
                            "VALUES (:id,:project_id,:actor_id,:label) "
                            "RETURNING id,project_id,label"
                        ),
                        {
                            "id": account_id,
                            "project_id": project_id,
                            "actor_id": self.actor_id,
                            "label": data.label,
                        },
                    )
                    .mappings()
                    .one()
                )
        except Exception:
            try:
                saved = self._readback_account(project_id, account_id)
            except SQLAlchemyError:
                # Commit outcome is unknown. Keep the key until a later owner
                # readback or startup orphan reconciliation can settle it.
                raise Unavailable("GitHub account save outcome unknown") from None
            if saved is not None:
                if saved["actor_id"] != self.actor_id or saved["label"] != data.label:
                    raise Unavailable("GitHub account save readback mismatched") from None
                if not self.secrets.present(account_id):
                    raise Unavailable("GitHub credential storage unavailable") from None
                return {
                    "id": saved["id"],
                    "project_id": saved["project_id"],
                    "label": saved["label"],
                    "credential_present": True,
                }
            try:
                self.secrets.remove(account_id)
            except OSError:
                raise Unavailable("GitHub credential cleanup failed") from None
            raise
        return dict(row, credential_present=True)

    def _readback_account(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id,project_id,actor_id,label FROM github_accounts "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": account_id},
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    def _credential(self, project_id: UUID, account_id: UUID) -> str:
        with self.engine.connect() as connection:
            found = connection.scalar(
                text("SELECT 1 FROM github_accounts WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": account_id},
            )
        if found is None:
            raise NotFound("GitHub account not found")
        return self.secrets.read(account_id)

    def repositories(self, project_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
        return list_repositories(self._credential(project_id, account_id), self.settings)

    def read_github_file(
        self, project_id: UUID, source_id: UUID, revision: str, path: str, *, limit: int, deadline: float
    ) -> bytes:
        from onefoundry.source_github_file import read

        return read(self, project_id, source_id, revision, path, limit=limit, deadline=deadline)

    def github_source_snapshot(self, project_id: UUID, source_id: UUID) -> dict[str, Any]:
        """Read current branch metadata for one saved source without changing its pinned revision."""
        self._active_project(project_id)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT s.kind,s.account_id,s.owner,s.repo,s.branch,s.revision,"
                        "s.created_at AS source_created_at,a.created_at AS account_created_at "
                        "FROM project_sources s LEFT JOIN github_accounts a ON "
                        "a.project_id=s.project_id AND a.id=s.account_id "
                        "WHERE s.project_id=:project_id AND s.id=:source_id"
                    ),
                    {"project_id": project_id, "source_id": source_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise NotFound("selected source not found")
        if row["kind"] != "github" or row["account_id"] is None:
            raise Conflict("selected source must be a saved GitHub repository")
        credential = self._credential(project_id, row["account_id"])
        branch, current_revision = resolve_revision(
            credential, self.settings, row["owner"], row["repo"], row["branch"]
        )
        if branch != row["branch"]:
            raise Conflict("GitHub returned a different branch")
        return {
            "project_id": project_id,
            "source_id": source_id,
            "account_id": row["account_id"],
            "source_created_at": row["source_created_at"],
            "account_created_at": row["account_created_at"],
            "owner": row["owner"],
            "repo": row["repo"],
            "branch": branch,
            "saved_revision": row["revision"],
            "current_revision": current_revision,
            "matches_saved_revision": current_revision == row["revision"],
        }

    def add_github(self, project_id: UUID, data: GitHubSourceCreate) -> dict[str, Any]:
        self._active_project(project_id)
        credential = self._credential(project_id, data.account_id)
        branch, revision = resolve_revision(credential, self.settings, data.owner, data.repo, data.branch)
        return self._insert_source(
            project_id,
            {
                "kind": "github",
                "display_name": f"{data.owner}/{data.repo}",
                "root_id": None,
                "relative_path": None,
                "account_id": data.account_id,
                "owner": data.owner,
                "repo": data.repo,
                "branch": branch,
                "revision": revision,
            },
        )

    def _insert_source(
        self, project_id: UUID, values: dict[str, Any], *, connection: Connection | None = None
    ) -> dict[str, Any]:
        with self.engine.begin() if connection is None else nullcontext(connection) as transaction:
            status = transaction.scalar(
                text("SELECT status FROM projects WHERE id=:id FOR SHARE"), {"id": project_id}
            )
            if status is None:
                raise NotFound("project not found")
            if status != "active":
                raise Conflict("restore this project before changing sources")
            row = (
                transaction.execute(
                    text(
                        "INSERT INTO project_sources "  # noqa: S608
                        "(id,project_id,actor_id,kind,display_name,root_id,relative_path,account_id,"
                        "owner,repo,branch,revision) VALUES "
                        "(:id,:project_id,:actor_id,:kind,:display_name,:root_id,:relative_path,"
                        ":account_id,:owner,:repo,:branch,:revision) "
                        f"RETURNING {SOURCE_COLUMNS}"
                    ),
                    {"id": uuid4(), "project_id": project_id, "actor_id": self.actor_id, **values},
                )
                .mappings()
                .one()
            )
        return dict(row, availability="verified_at_selection")

    def remove_source(self, project_id: UUID, source_id: UUID) -> None:
        with self.engine.begin() as connection:
            status = connection.scalar(
                text("SELECT status FROM projects WHERE id=:id FOR SHARE"), {"id": project_id}
            )
            if status is None:
                raise NotFound("project not found")
            if status != "active":
                raise Conflict("restore this project before changing sources")
            deleted = connection.scalar(
                text("DELETE FROM project_sources WHERE project_id=:project_id AND id=:id RETURNING id"),
                {"project_id": project_id, "id": source_id},
            )
            if deleted is None:
                raise NotFound("repository source not found")

    def remove_account(self, project_id: UUID, account_id: UUID) -> None:
        self._active_project(project_id)
        try:
            with self.engine.begin() as connection:
                status = connection.scalar(
                    text("SELECT status FROM projects WHERE id=:id FOR SHARE"), {"id": project_id}
                )
                if status is None:
                    raise NotFound("project not found")
                if status != "active":
                    raise Conflict("restore this project before changing sources")
                bound = connection.scalar(
                    text("SELECT 1 FROM project_sources WHERE project_id=:project_id AND account_id=:id"),
                    {"project_id": project_id, "id": account_id},
                )
                if bound is not None:
                    raise Conflict("remove this account's saved repository sources first")
                deleted = connection.scalar(
                    text("DELETE FROM github_accounts WHERE project_id=:project_id AND id=:id RETURNING id"),
                    {"project_id": project_id, "id": account_id},
                )
                if deleted is None:
                    raise NotFound("GitHub account not found")
        except IntegrityError:
            raise Conflict("remove this account's saved repository sources first") from None
        # Database deletion is durable. A file failure reports 503; startup
        # reconciliation removes the orphan after owner readback.
        try:
            self.secrets.remove(account_id)
        except OSError:
            raise Unavailable("GitHub credential removal failed") from None
