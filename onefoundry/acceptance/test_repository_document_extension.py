"""Independent acceptance for bounded reads of real-sized architecture documents.

This explicit baseline/candidate oracle is deliberately outside the ordinary suite
until the separate OneFoundry Engineer implements the missing capability.
"""

import hashlib
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


def test_range_reader_is_registered_and_obeys_repository_policy(tmp_path: Path) -> None:
    assert "repository_file_range_v1" in registered_tool_ids()
    engine, settings, _context, _source, _repo, _revision = _fixture(tmp_path)
    try:
        for enabled in (False, True):
            settings.team_repository_read_enabled = enabled
            assert (
                team_tool_readiness(
                    ["repository_file_range_v1"], role_name="Engineer", role_kind="builtin", settings=settings
                )[0]
                is enabled
            )
    finally:
        engine.dispose()


def test_large_committed_document_is_read_in_bounded_exact_pages(tmp_path: Path) -> None:
    owner = import_module("onefoundry.role_repository_range")
    engine, settings, context, source, repo, _revision = _fixture(tmp_path)
    body = "# Architecture\n" + ("Public interface → API → scoped retrieval.\n" * 1200)
    assert len(body.encode()) > settings.live_text_max_output_bytes
    try:
        (repo / "ARCHITECTURE.md").write_text(body)
        _git(repo, "add", "ARCHITECTURE.md")
        _git(repo, "commit", "-qm", "large architecture document")
        revision = _git(repo, "rev-parse", "HEAD")
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE project_sources SET revision=:r WHERE id=:id"),
                {"r": revision, "id": str(source)},
            )
        context = replace(
            context,
            snapshot_tools=frozenset({"repository_file_range_v1"}),
            current_tools=frozenset({"repository_file_range_v1"}),
        )

        def read(offset: int = 0) -> dict:
            args = owner.parse_repository_range(
                {"source_id": str(source), "path": "ARCHITECTURE.md", "offset": offset}
            )
            with engine.connect() as connection:
                result = owner.repository_range(connection, context, args, settings)
            owner.validate_repository_range(result)
            return result

        offset = 0
        parts = []
        for _ in range(len(body) + 1):
            result = read(offset)
            assert result["found"] and result["revision"] == revision
            assert result["sha256"] == hashlib.sha256(body.encode()).hexdigest()
            assert result["byte_size"] == len(body.encode()) and result["offset"] == offset
            assert len(result["text"].encode()) <= settings.live_text_max_output_bytes // 2
            assert body[offset : offset + len(result["text"])] == result["text"]
            parts.append(result["text"])
            if result["next_offset"] is None:
                break
            assert result["next_offset"] == offset + len(result["text"]) and result["next_offset"] > offset
            offset = result["next_offset"]
        else:
            pytest.fail("range cursor never completed")
        assert "".join(parts) == body and len(parts) > 1
        assert read(len(body))["text"] == ""
        for offset in (-1, len(body) + 1):
            with pytest.raises(ValueError):
                read(offset)
        for path in ("../outside", ".env", ".git/config", "/etc/passwd"):
            with pytest.raises(ValueError):
                owner.parse_repository_range({"source_id": str(source), "path": path, "offset": 0})
        (repo / "ARCHITECTURE.md").write_text("uncommitted operator edit")
        assert read()["text"] == parts[0]
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
        _git(repo, "add", "ARCHITECTURE.md")
        _git(repo, "commit", "-qm", "revision changed")
        with pytest.raises(RepositoryReadError):
            read()
    finally:
        engine.dispose()
