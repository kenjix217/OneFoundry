"""Safe disposable repository fixture shared by reader tests and coding oracles."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from onefoundry.role_tool_admission import RunContext
from onefoundry.settings import Settings


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - disposable test checkout only
        ["/usr/bin/git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _context(project_id: UUID) -> RunContext:
    return RunContext(
        project_id=project_id,
        run_id=uuid4(),
        work_order_id=uuid4(),
        profile_id=uuid4(),
        snapshot_tools=frozenset({"repository_file_v1"}),
        current_tools=frozenset({"repository_file_v1"}),
        snapshot_memory_scope="project",
        current_memory_scope="project",
        max_items=5,
        current_role_name="Researcher",
        current_role_kind="builtin",
        snapshot_role_name="Researcher",
    )


def _fixture(
    tmp_path: Path, content: bytes = b"answer = 42\n"
) -> tuple[Engine, Settings, RunContext, UUID, Path, str]:
    root = tmp_path / "source-root"
    root.mkdir()
    repo = root / "code"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "answer.py").write_bytes(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "saved")
    revision = _git(repo, "rev-parse", "HEAD")
    project_id, source_id = uuid4(), uuid4()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE project_sources (id TEXT,project_id TEXT,kind TEXT,root_id TEXT,"
                "relative_path TEXT,branch TEXT,revision TEXT,created_at TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO project_sources VALUES "
                "(:id,:project,'local','workspace','code','main',:revision,'now')"
            ),
            {"id": str(source_id), "project": str(project_id), "revision": revision},
        )
    return (
        engine,
        Settings(db_password=str(uuid4()), local_source_root=str(root)),
        _context(project_id),
        source_id,
        repo,
        revision,
    )
