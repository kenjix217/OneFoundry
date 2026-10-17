"""Verify an offline v3 source snapshot and materialize a disposable Git tree.

The caller owns the context lifetime. No repository path remains valid after exit.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT_PATH = re.compile(r"snapshots/([0-9a-f]{64})\.json\Z")
MAX_SNAPSHOT_BYTES = 16_777_216
MAX_FILES = 1_000
MAX_CONTENT_BYTES = 8_388_608


class SnapshotMaterializationError(ValueError):
    """An untrusted snapshot failed verification."""


@dataclass(frozen=True)
class MaterializedSnapshot:
    repository_path: Path
    source_tree: str
    snapshot_sha256: str
    file_count: int
    total_bytes: int


@dataclass
class _PathNode:
    children: dict[str, tuple[str, _PathNode]]
    is_file: bool = False


def _fail(reason: str) -> NoReturn:
    raise SnapshotMaterializationError(reason)


def _read_snapshot(root: Path, relative: str, expected_sha256: str, expected_size: int) -> bytes:
    matched = _SNAPSHOT_PATH.fullmatch(relative)
    if (
        matched is None
        or _SHA256.fullmatch(expected_sha256) is None
        or matched.group(1) != expected_sha256
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or expected_size > MAX_SNAPSHOT_BYTES
    ):
        _fail("snapshot_reference_invalid")
    descriptors: list[int] = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        directory = os.open("snapshots", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        descriptors.append(directory)
        handle = os.open(matched.group(1) + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        descriptors.append(handle)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            _fail("snapshot_size_mismatch")
        raw = bytearray()
        while len(raw) <= expected_size:
            chunk = os.read(handle, min(65_536, expected_size + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != expected_size:
            _fail("snapshot_size_mismatch")
        data = bytes(raw)
    except OSError as exc:
        raise SnapshotMaterializationError("snapshot_unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _fail("snapshot_hash_mismatch")
    return data


def _git_hash(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode() + content).hexdigest()  # noqa: S324


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not path or unicodedata.normalize("NFC", path) != path:
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    for segment in path.split("/"):
        if (
            not segment
            or segment in {".", ".."}
            or segment.casefold() == ".git"
            or any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in segment)
        ):
            return False
    return True


def _verify_bundle(data: bytes) -> tuple[str, list[tuple[str, str, bytes]], int]:
    try:
        decoded = data.decode("utf-8", errors="strict")
        value: object = json.loads(decoded)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SnapshotMaterializationError("snapshot_json_invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "kind", "source_tree", "files"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 3
        or value["kind"] != "github_fixture_snapshot_bundle_v3"
        or not isinstance(value["source_tree"], str)
        or _SHA1.fullmatch(value["source_tree"]) is None
        or not isinstance(value["files"], list)
        or len(value["files"]) > MAX_FILES
    ):
        _fail("snapshot_schema_invalid")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise SnapshotMaterializationError("snapshot_json_invalid") from exc
    if canonical != data:
        _fail("snapshot_not_canonical")
    bundle = cast("dict[str, object]", value)
    source_tree = cast("str", bundle["source_tree"])
    rows = cast("list[object]", bundle["files"])
    result: list[tuple[str, str, bytes]] = []
    previous: bytes | None = None
    paths = _PathNode({})
    total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "mode", "git_blob_sha", "content_base64"}:
            _fail("snapshot_row_invalid")
        row = cast("dict[str, object]", row)
        path, mode, blob_sha, encoded = (
            row[key] for key in ("path", "mode", "git_blob_sha", "content_base64")
        )
        if (
            not _valid_path(path)
            or not isinstance(mode, str)
            or mode not in {"100644", "100755"}
            or not isinstance(blob_sha, str)
            or _SHA1.fullmatch(blob_sha) is None
            or not isinstance(encoded, str)
        ):
            _fail("snapshot_row_invalid")
        path = cast("str", path)
        path_bytes = path.encode("utf-8")
        if previous is not None and path_bytes <= previous:
            _fail("snapshot_path_order_invalid")
        previous = path_bytes
        node = paths
        parts = path.split("/")
        for index, segment in enumerate(parts):
            if node.is_file:
                _fail("snapshot_path_alias")
            alias = unicodedata.normalize("NFD", segment).casefold()
            existing = node.children.get(alias)
            if existing is None:
                child = _PathNode({})
                node.children[alias] = (segment, child)
            else:
                original, child = existing
                if original != segment:
                    _fail("snapshot_path_alias")
            node = child
            if index == len(parts) - 1:
                if node.is_file or node.children:
                    _fail("snapshot_path_alias")
                node.is_file = True
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SnapshotMaterializationError("snapshot_base64_invalid") from exc
        if base64.b64encode(content).decode("ascii") != encoded:
            _fail("snapshot_base64_invalid")
        total += len(content)
        if total > MAX_CONTENT_BYTES:
            _fail("snapshot_content_limit")
        if _git_hash("blob", content) != blob_sha:
            _fail("snapshot_blob_mismatch")
        result.append((path, mode, content))
    return source_tree, result, total


def _git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        _fail("git_materialization_failed")
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        completed = subprocess.run(  # noqa: S603 - resolved Git command and controlled arguments
            [executable, "-c", "core.autocrlf=false", *arguments],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotMaterializationError("git_materialization_failed") from exc
    return completed.stdout.strip()


@contextmanager
def materialize_snapshot(
    snapshot_root: Path,
    snapshot_relative_path: str,
    expected_sha256: str,
    expected_size: int,
    work_root: Path,
) -> Iterator[MaterializedSnapshot]:
    """Yield a verified disposable repository; remove it when the context exits.

    ``snapshot_root`` and ``work_root`` are trusted caller-selected directories.
    Only ``snapshots/<sha256>.json`` is accepted beneath the snapshot root.
    """
    raw = _read_snapshot(snapshot_root, snapshot_relative_path, expected_sha256, expected_size)
    source_tree, files, total = _verify_bundle(raw)
    with tempfile.TemporaryDirectory(prefix="onefoundry-github-snapshot-", dir=work_root) as temporary:
        repository = Path(temporary)
        _git(repository, "init", "--quiet")
        for path, mode, content in files:
            destination = repository.joinpath(*path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
            destination.chmod(0o755 if mode == "100755" else 0o644)
        for path, mode, content in files:
            blob_sha = _git(repository, "hash-object", "-w", "--no-filters", "--", path)
            if blob_sha != _git_hash("blob", content):
                _fail("materialized_blob_mismatch")
            _git(repository, "update-index", "--add", "--cacheinfo", f"{mode},{blob_sha},{path}")
        actual_tree = _git(repository, "write-tree")
        if actual_tree != source_tree:
            _fail("snapshot_tree_mismatch")
        yield MaterializedSnapshot(repository, source_tree, expected_sha256, len(files), total)
