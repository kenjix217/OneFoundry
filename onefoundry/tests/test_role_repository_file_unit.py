"""A role may read only bounded, safe text from a saved local Git commit."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from onefoundry.role_repository_file import (
    RepositoryReadError,
    parse_repository_file,
    repository_file,
    validate_repository_file,
)
from onefoundry.role_tool_admission import RunContext, ToolRepositoryReadFailure
from onefoundry.settings import Settings
from tests.repository_read_fixture import _context as _context
from tests.repository_read_fixture import _fixture as _fixture
from tests.repository_read_fixture import _git as _git


def _read(
    engine: Engine, settings: Settings, context: RunContext, source_id: UUID, path: str
) -> dict[str, object]:
    with engine.connect() as connection:
        return repository_file(
            connection, context, parse_repository_file({"source_id": str(source_id), "path": path}), settings
        )


def test_reads_only_saved_commit_text_even_when_worktree_changes(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, revision = _fixture(tmp_path)
    (repo / "src" / "answer.py").write_text("answer = 999\n")
    result = _read(engine, settings, context, source_id, "src/answer.py")
    assert result == {
        "source_id": str(source_id),
        "found": True,
        "revision": revision,
        "path": "src/answer.py",
        "sha256": hashlib.sha256(b"answer = 42\n").hexdigest(),
        "byte_size": 12,
        "text": "answer = 42\n",
        "truncated": False,
    }
    validate_repository_file(result)
    engine.dispose()


def test_source_must_belong_to_project_and_match_saved_head(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET project_id=:other WHERE id=:id"),
            {"other": str(uuid4()), "id": str(source_id)},
        )
    assert _read(engine, settings, context, source_id, "src/answer.py") == {
        "source_id": str(source_id),
        "found": False,
    }
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET project_id=:project WHERE id=:id"),
            {"project": str(context.project_id), "id": str(source_id)},
        )
    (repo / "src" / "answer.py").write_text("answer = 7\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "advance")
    with pytest.raises(RepositoryReadError, match="drift") as drift:
        _read(engine, settings, context, source_id, "src/answer.py")
    assert drift.value.code == "source_revision_drifted"
    engine.dispose()


def test_saved_repository_directory_can_have_space(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(tmp_path)
    repo.rename(repo.with_name("team repo"))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET relative_path='team repo' WHERE id=:id"),
            {"id": str(source_id)},
        )
    assert _read(engine, settings, context, source_id, "src/answer.py")["text"] == "answer = 42\n"
    engine.dispose()


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../answer.py",
        "src/../answer.py",
        "src//answer.py",
        "src\\answer.py",
        ".git/config",
        "src/.git/config",
        "src/.env",
        "src/id_rsa",
        ".npmrc",
        "src/.pypirc",
        "bad\x00name",
    ],
)
def test_parser_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        parse_repository_file({"source_id": str(uuid4()), "path": path})


def test_rejects_symlink_binary_oversize_and_secret_text(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(tmp_path)
    (repo / "link").symlink_to("src/answer.py")
    (repo / "binary.bin").write_bytes(b"abc\x00def")
    (repo / "large.txt").write_bytes(b"a" * (settings.live_text_max_output_bytes + 1))
    (repo / "secret.txt").write_text("api_key = supersecretvalue\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "other files")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET revision=:revision WHERE id=:id"),
            {"revision": _git(repo, "rev-parse", "HEAD"), "id": str(source_id)},
        )
    expected_codes = {
        "link": "source_file_unreadable",
        "binary.bin": "source_content_unsupported",
        "large.txt": "source_file_too_large",
        "secret.txt": "source_content_protected",
    }
    for path, code in expected_codes.items():
        with pytest.raises(RepositoryReadError) as error:
            _read(engine, settings, context, source_id, path)
        assert error.value.code == code
    engine.dispose()


def test_rejects_database_uri_credentials_in_source_text(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(
        tmp_path, b"DB_URI=postgresql://alice:abc123@db.internal/company\n"
    )
    with pytest.raises(RepositoryReadError, match="secret") as error:
        _read(engine, settings, context, source_id, "src/answer.py")
    assert error.value.code == "source_content_protected"
    engine.dispose()


def test_repository_failure_guidance_is_static_and_bounded() -> None:
    blocked = ToolRepositoryReadFailure("source_content_protected", "repository_file_v1")
    assert blocked.code == "source_content_protected"
    assert "secret screening" in blocked.next_step
    assert "range read" in blocked.next_step
    oversized = ToolRepositoryReadFailure("source_file_too_large", "repository_file_v1")
    assert "repository_file_range_v1" in oversized.next_step
    range_oversized = ToolRepositoryReadFailure("source_file_too_large", "repository_file_range_v1")
    assert "range reader's complete-file limit" in range_oversized.next_step
    unknown = ToolRepositoryReadFailure("untrusted owner text", "repository_file_v1")
    assert unknown.code == "source_unavailable"
    assert "untrusted owner text" not in str(unknown) + unknown.next_step
