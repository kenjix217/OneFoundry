"""Independent acceptance for the absent repository-directory capability.

Run explicitly against the baseline and the candidate. This remains outside the
ordinary suite until the separate OneFoundry Engineer implements the extension.
The task needs discovery of committed documentation paths without guessing names.
"""

from dataclasses import replace
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from onefoundry.role_repository_file import RepositoryReadError
from onefoundry.role_tool_admission import registered_tool_ids
from onefoundry.task_session_tool_readiness import team_tool_readiness
from tests.repository_read_fixture import _fixture, _git


def test_registered_and_respects_repository_activation(tmp_path: Path) -> None:
    assert "repository_tree_v1" in registered_tool_ids()
    engine, settings, _context, _source, _repo, _revision = _fixture(tmp_path)
    try:
        settings.team_repository_read_enabled = False
        assert not team_tool_readiness(
            ["repository_tree_v1"], role_name="Engineer", role_kind="builtin", settings=settings
        )[0]
        settings.team_repository_read_enabled = True
        assert team_tool_readiness(
            ["repository_tree_v1"], role_name="Engineer", role_kind="builtin", settings=settings
        )[0]
    finally:
        engine.dispose()


def test_exact_committed_directory_paging_and_private_paths(tmp_path: Path) -> None:
    owner = import_module("onefoundry.role_repository_tree")
    engine, settings, context, source, repo, _revision = _fixture(tmp_path)
    try:
        (repo / "ARCHITECTURE.md").write_text("Local project architecture.\n")
        (repo / "LICENSE").write_text("Private fixture.\n")
        (repo / ".env").write_text("FIXTURE_PRIVATE_CONFIGURATION=true\n")
        (repo / "link").symlink_to("ARCHITECTURE.md")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "documentation")
        revision = _git(repo, "rev-parse", "HEAD")
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE project_sources SET revision=:r WHERE id=:id"),
                {"r": revision, "id": str(source)},
            )
        (repo / "UNCOMMITTED.md").write_text("Not approved source content.\n")
        context = replace(
            context,
            max_items=2,
            snapshot_tools=frozenset({"repository_tree_v1"}),
            current_tools=frozenset({"repository_tree_v1"}),
        )

        def read(directory: str = "", after: str = "") -> dict:
            args = owner.parse_repository_tree(
                {"source_id": str(source), "directory": directory, "after": after}
            )
            with engine.connect() as connection:
                result = owner.repository_tree(connection, context, args, settings)
            owner.validate_repository_tree(result)
            return result

        first = read()
        assert first["found"] and first["revision"] == revision
        assert [item["path"] for item in first["entries"]] == ["ARCHITECTURE.md", "LICENSE"]
        assert first["has_more"] and first["next_cursor"] == "LICENSE"
        second = read(after=first["next_cursor"])
        assert [item["path"] for item in second["entries"]] == ["src"]
        assert second["entries"][0]["kind"] == "directory" and not second["has_more"]
        nested = read("src")
        assert [item["path"] for item in nested["entries"]] == ["src/answer.py"]
        assert nested["entries"][0]["kind"] == "file"
        for unsafe in ("..", "/etc", ".git", ".env", "src/../.env"):
            with pytest.raises(ValueError):
                read(unsafe)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE project_sources SET project_id=:p WHERE id=:id"),
                {"p": str(uuid4()), "id": str(source)},
            )
        assert read() == {"source_id": str(source), "found": False}
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE project_sources SET project_id=:p WHERE id=:id"),
                {"p": str(context.project_id), "id": str(source)},
            )
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "changed source")
        with pytest.raises(RepositoryReadError):
            read()
    finally:
        engine.dispose()
