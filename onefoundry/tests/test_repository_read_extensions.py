"""Registered tree pages and bounded document reads."""

import base64
import hashlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from onefoundry import repository_github, source_store
from onefoundry import role_repository_file as file
from onefoundry import role_repository_range as document
from onefoundry import role_repository_tree as tree
from onefoundry.role_repository_file import RepositoryReadError
from onefoundry.role_tool_admission import LIVE_SDK_EXCLUDED_IDS, READ_TOOLS, RunContext
from onefoundry.settings import Settings
from tests.repository_read_fixture import _fixture, _git


def test_local_tree_pages_and_exact_character_ranges(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, revision = _fixture(
        tmp_path, "élan \U0001f600\nnext\n".encode()
    )
    (repo / "src" / "z.py").write_text("more\n")
    (repo / ".env").write_text("do not list")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "next")
    updated = _git(repo, "rev-parse", "HEAD")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET revision=:r WHERE id=:id"), {"r": updated, "id": str(source_id)}
        )
    context = replace(context, max_items=1)
    with engine.connect() as connection:
        first = tree.repository_tree(
            connection, context, tree.parse_repository_tree({"source_id": str(source_id)}), settings
        )
        tree.validate_repository_tree(first)
        assert first["revision"] == updated
        assert [item["path"] for item in first["entries"]] == ["src"]
        assert len(first["entries"][0]["sha"]) == 40
        assert first["next_cursor"] is None
        arguments = tree.parse_repository_tree({"source_id": str(source_id), "directory": "src"})
        page = tree.repository_tree(connection, context, arguments, settings)
        tree.validate_repository_tree(page)
        assert [entry["path"] for entry in page["entries"]] == ["src/answer.py"]
        assert page["next_cursor"] == "src/answer.py"
        next_page = tree.repository_tree(
            connection, context, {**arguments, "after": page["next_cursor"]}, settings
        )
        tree.validate_repository_tree(next_page)
        assert [entry["path"] for entry in next_page["entries"]] == ["src/z.py"]
        assert next_page["next_cursor"] is None
        excerpt = document.repository_range(
            connection,
            context,
            document.parse_repository_range(
                {"source_id": str(source_id), "path": "src/answer.py", "offset": 1}
            ),
            settings,
        )
    assert excerpt["text"] == "lan \U0001f600\nnext\n"
    assert excerpt["offset"] == 1 and excerpt["next_offset"] is None
    assert excerpt["sha256"] == hashlib.sha256("élan \U0001f600\nnext\n".encode()).hexdigest()
    assert excerpt["byte_size"] == len("élan \U0001f600\nnext\n".encode())
    document.validate_repository_range(excerpt)
    assert revision != updated


def test_readers_reject_bad_inputs_secret_and_changed_commit(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(tmp_path)
    for invalid in (
        {"source_id": str(source_id), "path": ".env"},
        {"source_id": str(source_id), "path": "src/../answer.py"},
    ):
        with pytest.raises(ValueError):
            tree.parse_repository_tree({"source_id": invalid["source_id"], "directory": invalid["path"]})
        with pytest.raises(ValueError):
            document.parse_repository_range({**invalid, "offset": 0})
    with pytest.raises(ValueError):
        document.parse_repository_range({"source_id": str(source_id), "path": "src/answer.py", "offset": -1})
    with engine.connect() as connection:
        assert not tree.repository_tree(
            connection, context, tree.parse_repository_tree({"source_id": str(uuid4())}), settings
        )["found"]
        with pytest.raises(ValueError):
            document.repository_range(
                connection,
                context,
                document.parse_repository_range(
                    {"source_id": str(source_id), "path": "src/answer.py", "offset": 999}
                ),
                settings,
            )
    (repo / "src" / "answer.py").write_text("changed\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-qm", "changed")
    with engine.connect() as connection, pytest.raises(RepositoryReadError):
        tree.repository_tree(
            connection, context, tree.parse_repository_tree({"source_id": str(source_id)}), settings
        )


def test_full_document_screening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(tmp_path)
    marker = str(uuid4())
    (repo / "src" / "answer.py").write_text("public text\n" * 800 + marker)
    _git(repo, "add", "src/answer.py")
    _git(repo, "commit", "-qm", "document")
    revision = _git(repo, "rev-parse", "HEAD")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET revision=:r WHERE id=:id"),
            {"r": revision, "id": str(source_id)},
        )

    def contains_marker(content: str) -> bool:
        return marker in content

    monkeypatch.setattr(file, "contains_sensitive_text", contains_marker)
    try:
        with engine.connect() as connection, pytest.raises(RepositoryReadError):
            document.repository_range(
                connection,
                context,
                document.parse_repository_range(
                    {"source_id": str(source_id), "path": "src/answer.py", "offset": 0}
                ),
                settings,
            )
    finally:
        engine.dispose()


def test_registered_contracts_are_distinct_and_legacy_stays_excluded() -> None:
    for tool_id in ("repository_tree_v1", "repository_file_range_v1"):
        assert READ_TOOLS[tool_id].stable_snapshot
        assert tool_id in LIVE_SDK_EXCLUDED_IDS


def test_github_exact_tree_and_bounded_blob(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    content = ("Committable architecture → scoped read.\n" * 320).encode()
    blob_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content, usedforsecurity=False).hexdigest()
    commit_sha, root_sha, nested_sha = "a" * 40, "b" * 40, "c" * 40
    objects: dict[tuple[str, str], dict[str, Any]] = {
        ("commits", commit_sha): {"sha": commit_sha, "tree": {"sha": root_sha}},
        ("trees", root_sha): {
            "sha": root_sha,
            "truncated": False,
            "tree": [
                {"path": "src", "mode": "040000", "type": "tree", "sha": nested_sha},
                {"path": ".env", "mode": "100644", "type": "blob", "sha": blob_sha, "size": len(content)},
            ],
        },
        ("trees", nested_sha): {
            "sha": nested_sha,
            "truncated": False,
            "tree": [
                {
                    "path": "ARCHITECTURE.md",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                    "size": len(content),
                }
            ],
        },
        ("blobs", blob_sha): {
            "sha": blob_sha,
            "encoding": "base64",
            "size": len(content),
            "content": base64.b64encode(content).decode(),
        },
    }

    def get(url: str, credential: str, settings: Settings, *, timeout_seconds: float) -> dict[str, Any]:
        assert credential == account_credential and timeout_seconds > 0
        kind, object_id = url.rsplit("/", 2)[-2:]
        return objects[kind, object_id]

    account_credential = str(uuid4())
    monkeypatch.setattr(repository_github, "_get", get)
    settings = Settings(db_password=str(uuid4()))
    deadline = time.monotonic() + settings.github_timeout_seconds
    root = repository_github.read_tree(
        "owner", "repo", commit_sha, ".", account_credential, settings, deadline=deadline
    )
    assert [entry["path"] for entry in root] == ["src"]
    assert root[0]["sha"] == nested_sha
    objects["trees", root_sha]["truncated"] = True
    with pytest.raises(RepositoryReadError):
        repository_github.read_tree(
            "owner", "repo", commit_sha, ".", account_credential, settings, deadline=deadline
        )
    objects["trees", root_sha]["truncated"] = False
    nested = repository_github.read_tree(
        "owner", "repo", commit_sha, "src", account_credential, settings, deadline=deadline
    )
    assert [entry["path"] for entry in nested] == ["src/ARCHITECTURE.md"]
    assert (
        repository_github.read_blob(
            "owner",
            "repo",
            commit_sha,
            "src/ARCHITECTURE.md",
            account_credential,
            settings,
            limit=settings.input_file_max_text_bytes,
            deadline=deadline,
        )
        == content
    )
    with pytest.raises(RepositoryReadError):
        repository_github.read_blob(
            "owner",
            "repo",
            commit_sha,
            "src/ARCHITECTURE.md",
            account_credential,
            settings,
            limit=10,
            deadline=deadline,
        )

    engine, settings, context, source_id, _repo, _revision = _fixture(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET kind='github',revision=:r WHERE id=:id"),
            {"r": commit_sha, "id": str(source_id)},
        )

    def github_file(
        store: source_store.SourceStore,
        project_id: UUID,
        saved_source_id: UUID,
        revision: str,
        path: str,
        *,
        limit: int,
        deadline: float,
    ) -> bytes:
        assert project_id == context.project_id and saved_source_id == source_id
        return repository_github.read_blob(
            "owner",
            "repo",
            revision,
            path,
            account_credential,
            settings,
            limit=limit,
            deadline=deadline,
        )

    def github_tree(
        connection: Connection,
        run_context: RunContext,
        saved_source_id: str,
        source: dict[str, Any],
        path: str,
        configured_settings: Settings,
    ) -> list[dict[str, Any]]:
        assert saved_source_id == str(source_id) and run_context.project_id == context.project_id
        return repository_github.read_tree(
            "owner",
            "repo",
            source["revision"],
            path,
            account_credential,
            configured_settings,
            deadline=time.monotonic() + configured_settings.github_timeout_seconds,
        )

    monkeypatch.setattr(source_store.SourceStore, "read_github_file", github_file)
    monkeypatch.setattr(tree, "_github_entries", github_tree)
    context = replace(context, max_items=1)
    try:
        with engine.connect() as connection:
            tree_result = tree.repository_tree(
                connection, context, tree.parse_repository_tree({"source_id": str(source_id)}), settings
            )
            tree.validate_repository_tree(tree_result)
            assert tree_result["entries"][0]["kind"] == "directory"
            args = document.parse_repository_range(
                {"source_id": str(source_id), "path": "src/ARCHITECTURE.md", "offset": 0}
            )
            first = document.repository_range(connection, context, args, settings)
            document.validate_repository_range(first)
            assert first["sha256"] == hashlib.sha256(content).hexdigest()
            assert first["byte_size"] == len(content)
            assert first["next_offset"] is not None
            assert first["text"] == content.decode()[: first["next_offset"]]
    finally:
        engine.dispose()


def test_new_local_api_pages_unicode_and_rejects_invalid_offsets(tmp_path: Path) -> None:
    engine, settings, context, source_id, repo, _revision = _fixture(tmp_path)
    content = "é→ documentation\n" * 700
    (repo / "src" / "answer.py").write_text(content)
    _git(repo, "add", "src/answer.py")
    _git(repo, "commit", "-qm", "document")
    revision = _git(repo, "rev-parse", "HEAD")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE project_sources SET revision=:r WHERE id=:id"),
            {"r": revision, "id": str(source_id)},
        )
    context = replace(context, max_items=1)
    try:
        with engine.connect() as connection:
            tree_args = tree.parse_repository_tree({"source_id": str(source_id)})
            root = tree.repository_tree(connection, context, tree_args, settings)
            tree.validate_repository_tree(root)
            assert [item["path"] for item in root["entries"]] == ["src"]
            assert root["entries"][0]["kind"] == "directory"
            args = document.parse_repository_range(
                {"source_id": str(source_id), "path": "src/answer.py", "offset": 0}
            )
            parts = []
            while True:
                page = document.repository_range(connection, context, args, settings)
                document.validate_repository_range(page)
                assert page["sha256"] == hashlib.sha256(content.encode()).hexdigest()
                assert len(page["text"].encode()) <= settings.live_text_max_output_bytes // 2
                parts.append(page["text"])
                if page["next_offset"] is None:
                    break
                args["offset"] = page["next_offset"]
            assert "".join(parts) == content
            args["offset"] = len(content)
            assert document.repository_range(connection, context, args, settings)["text"] == ""
            args["offset"] += 1
            with pytest.raises(ValueError):
                document.repository_range(connection, context, args, settings)
    finally:
        engine.dispose()
