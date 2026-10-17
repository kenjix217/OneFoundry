"""Offline v3 snapshot materialization: exact bytes, paths, hashes and Git tree."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from runner.github_fixture_snapshot_materialize import (
    MAX_SNAPSHOT_BYTES,
    SnapshotMaterializationError,
    materialize_snapshot,
)


def git_hash(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode() + content).hexdigest()  # noqa: S324


def tree_hash(files: list[tuple[str, str, bytes]]) -> str:
    entries: dict[str, tuple[str, str, bytes] | dict[str, Any]] = {}
    for path, mode, content in files:
        node = entries
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[parts[-1]] = (path, mode, content)

    def hash_node(node: dict[str, Any]) -> str:
        encoded = bytearray()
        for name, item in sorted(
            node.items(), key=lambda pair: pair[0].encode() + (b"/" if isinstance(pair[1], dict) else b"")
        ):
            if isinstance(item, dict):
                mode, sha = "40000", hash_node(item)
            else:
                _, mode, content = item
                sha = git_hash("blob", content)
            encoded.extend(f"{mode} {name}\0".encode())
            encoded.extend(bytes.fromhex(sha))
        return git_hash("tree", bytes(encoded))

    return hash_node(entries)


def bundle(files: list[tuple[str, str, bytes]]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "kind": "github_fixture_snapshot_bundle_v3",
        "source_tree": tree_hash(files),
        "files": [
            {
                "path": path,
                "mode": mode,
                "git_blob_sha": git_hash("blob", content),
                "content_base64": base64.b64encode(content).decode(),
            }
            for path, mode, content in sorted(files, key=lambda row: row[0].encode())
        ],
    }


def snapshot(tmp_path: Path, value: object, *, raw: bytes | None = None) -> tuple[Path, str, str, int, Path]:
    root = tmp_path / "root"
    (root / "snapshots").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    data = (
        raw
        if raw is not None
        else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )
    digest = hashlib.sha256(data).hexdigest()
    relative = f"snapshots/{digest}.json"
    (root / relative).write_bytes(data)
    return root, relative, digest, len(data), work


@pytest.mark.parametrize(
    "files",
    [[], [("readme.txt", "100644", b"hello\n")], [("nested/run.sh", "100755", b"#!/bin/sh\n")]],
)
def test_materializes_verified_tree_and_cleans_up(
    tmp_path: Path, files: list[tuple[str, str, bytes]]
) -> None:
    args = snapshot(tmp_path, bundle(files))
    executable = shutil.which("git")
    assert executable is not None
    with materialize_snapshot(*args) as receipt:
        assert receipt.source_tree == tree_hash(files)
        assert receipt.file_count == len(files)
        assert receipt.total_bytes == sum(len(content) for _, _, content in files)
        assert (
            subprocess.check_output(  # noqa: S603 - resolved Git verification command
                [executable, "write-tree"], cwd=receipt.repository_path, text=True
            ).strip()
            == receipt.source_tree
        )
        for path, mode, content in files:
            actual = receipt.repository_path / path
            assert actual.read_bytes() == content
            assert bool(actual.stat().st_mode & 0o111) == (mode == "100755")
        repository = receipt.repository_path
    assert not repository.exists()


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda value: value["files"][0].update(path="../escape"), "snapshot_row_invalid"),
        (lambda value: value["files"][0].update(path=".GIT/config"), "snapshot_row_invalid"),
        (lambda value: value["files"][0].update(mode="120000"), "snapshot_row_invalid"),
        (lambda value: value["files"][0].update(mode=[]), "snapshot_row_invalid"),
        (lambda value: value["files"][0].update(git_blob_sha="0" * 40), "snapshot_blob_mismatch"),
        (lambda value: value.update(source_tree="0" * 40), "snapshot_tree_mismatch"),
        (lambda value: value["files"].append(value["files"][0].copy()), "snapshot_path_order_invalid"),
        (lambda value: value["files"][0].update(content_base64="!!!"), "snapshot_base64_invalid"),
    ],
)
def test_rejects_invalid_bundle(tmp_path: Path, change: Any, reason: str) -> None:
    value = bundle([("file.txt", "100644", b"text")])
    change(value)
    with pytest.raises(SnapshotMaterializationError, match=reason):
        with materialize_snapshot(*snapshot(tmp_path, value)):
            pass


def test_rejects_casefold_alias(tmp_path: Path) -> None:
    value = bundle([("A.txt", "100644", b"a"), ("a.txt", "100644", b"b")])
    with pytest.raises(SnapshotMaterializationError, match="snapshot_path_alias"):
        with materialize_snapshot(*snapshot(tmp_path, value)):
            pass


@pytest.mark.parametrize(
    "files",
    [
        [("A/x", "100644", b"a"), ("a/y", "100644", b"b")],
        [("A", "100644", b"a"), ("a/x", "100644", b"b")],
    ],
)
def test_rejects_directory_aliases(tmp_path: Path, files: list[tuple[str, str, bytes]]) -> None:
    with pytest.raises(SnapshotMaterializationError, match="snapshot_path_alias"):
        with materialize_snapshot(*snapshot(tmp_path, bundle(files))):
            pass


def test_rejects_lone_surrogate_path(tmp_path: Path) -> None:
    value = bundle([("file", "100644", b"x")])
    value["files"][0]["path"] = "bad\ud800"
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(SnapshotMaterializationError, match="snapshot_json_invalid"):
        with materialize_snapshot(*snapshot(tmp_path, value, raw=raw)):
            pass


def test_rejects_tamper_truncation_and_noncanonical_bytes(tmp_path: Path) -> None:
    value = bundle([])
    root, relative, digest, size, work = snapshot(tmp_path, value)
    (root / relative).write_bytes(b"x" * size)
    with pytest.raises(SnapshotMaterializationError, match="snapshot_hash_mismatch"):
        with materialize_snapshot(root, relative, digest, size, work):
            pass
    (root / relative).write_bytes(b"x")
    with pytest.raises(SnapshotMaterializationError, match="snapshot_size_mismatch"):
        with materialize_snapshot(root, relative, digest, size, work):
            pass
    raw = json.dumps(value).encode()
    with pytest.raises(SnapshotMaterializationError, match="snapshot_not_canonical"):
        with materialize_snapshot(*snapshot(tmp_path / "other", value, raw=raw)):
            pass


def test_rejects_symlink_and_bad_reference(tmp_path: Path) -> None:
    root, relative, digest, size, work = snapshot(tmp_path, bundle([]))
    (root / relative).unlink()
    (root / relative).symlink_to("/etc/passwd")
    with pytest.raises(SnapshotMaterializationError, match="snapshot_unavailable"):
        with materialize_snapshot(root, relative, digest, size, work):
            pass
    with pytest.raises(SnapshotMaterializationError, match="snapshot_reference_invalid"):
        with materialize_snapshot(root, "../" + relative, digest, size, work):
            pass


def test_git_environment_cannot_redirect_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside.git"
    monkeypatch.setenv("GIT_DIR", str(outside))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "outside.index"))
    with materialize_snapshot(*snapshot(tmp_path, bundle([("file", "100644", b"x")]))):
        pass
    assert not outside.exists()
    assert not (tmp_path / "outside.index").exists()


def test_rejects_oversize_reference_and_content(tmp_path: Path) -> None:
    args = snapshot(tmp_path, bundle([]))
    with pytest.raises(SnapshotMaterializationError, match="snapshot_reference_invalid"):
        with materialize_snapshot(args[0], args[1], args[2], MAX_SNAPSHOT_BYTES + 1, args[4]):
            pass
    value = bundle([("large", "100644", os.urandom(8_388_609))])
    with pytest.raises(SnapshotMaterializationError, match="snapshot_content_limit"):
        with materialize_snapshot(*snapshot(tmp_path / "large", value)):
            pass
