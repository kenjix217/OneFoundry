"""Canonical, bounded source bundle for the fake HTTPS GitHub snapshot path.

The commit-to-tree link remains fixture asserted. Blob and tree bytes are
independently checked here, including when a stored bundle is read back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_FILES = 1000
MAX_CONTENT_BYTES = 8_388_608
MAX_BUNDLE_BYTES = 16_777_216


class BundleInvalid(ValueError):
    pass


@dataclass(frozen=True)
class BundleFile:
    path: str
    mode: str
    git_blob_sha: str
    content: bytes


@dataclass(frozen=True)
class BundleReadback:
    source_tree: str
    files: tuple[BundleFile, ...]
    total_bytes: int
    sha256: str
    byte_size: int


def _git_hash(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode() + content).hexdigest()  # noqa: S324


def _valid_path(path: str) -> bool:
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if not path or len(encoded) > 4096 or unicodedata.normalize("NFC", path) != path:
        return False
    for part in path.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part.casefold() == ".git"
            or "\\" in part
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
        ):
            return False
    return True


def _tree_hash(files: tuple[BundleFile, ...]) -> str:
    root: dict[str, object] = {}
    for item in files:
        node = root
        parts = item.path.split("/")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise BundleInvalid("file and directory path collision")
            node = child
        if parts[-1] in node:
            raise BundleInvalid("file and directory path collision")
        node[parts[-1]] = item

    def hash_node(node: dict[str, object]) -> str:
        rows: list[tuple[str, str, str]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                rows.append((name, "40000", hash_node(value)))
            elif isinstance(value, BundleFile):
                rows.append((name, value.mode, value.git_blob_sha))
            else:
                raise BundleInvalid("invalid bundle tree")
        raw = bytearray()
        for name, mode, sha in sorted(
            rows, key=lambda row: row[0].encode() + (b"/" if row[1] == "40000" else b"")
        ):
            raw.extend(f"{mode} {name}\0".encode())
            raw.extend(bytes.fromhex(sha))
        return _git_hash("tree", bytes(raw))

    return hash_node(root)


def _canonical(files: tuple[BundleFile, ...], source_tree: str) -> bytes:
    document = {
        "schema_version": 3,
        "kind": "github_fixture_snapshot_bundle_v3",
        "source_tree": source_tree,
        "files": [
            {
                "path": item.path,
                "mode": item.mode,
                "git_blob_sha": item.git_blob_sha,
                "content_base64": base64.b64encode(item.content).decode("ascii"),
            }
            for item in files
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_bundle(source_tree: str, files: list[BundleFile]) -> bytes:
    if any(not isinstance(item.path, str) or not _valid_path(item.path) for item in files):
        raise BundleInvalid("snapshot path invalid")
    ordered = tuple(sorted(files, key=lambda item: item.path.encode("utf-8")))
    _validate_files(source_tree, ordered)
    encoded = _canonical(ordered, source_tree)
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise BundleInvalid("snapshot bundle size limit")
    return encoded


def _validate_files(source_tree: str, files: tuple[BundleFile, ...]) -> int:
    if not isinstance(source_tree, str) or SHA40.fullmatch(source_tree) is None or len(files) > MAX_FILES:
        raise BundleInvalid("snapshot metadata invalid")
    prior = b""
    folded: dict[tuple[str, ...], tuple[str, ...]] = {}
    total = 0
    for item in files:
        if not isinstance(item.path, str) or not _valid_path(item.path):
            raise BundleInvalid("snapshot path invalid")
        encoded_path = item.path.encode("utf-8")
        parts = tuple(item.path.split("/"))
        if encoded_path <= prior:
            raise BundleInvalid("snapshot path collision")
        prior = encoded_path
        for depth in range(1, len(parts) + 1):
            exact = parts[:depth]
            alias = tuple(unicodedata.normalize("NFD", part).casefold() for part in exact)
            existing = folded.get(alias)
            if existing is not None and existing != exact:
                raise BundleInvalid("snapshot path collision")
            folded[alias] = exact
        if (
            not isinstance(item.mode, str)
            or item.mode not in {"100644", "100755"}
            or not isinstance(item.git_blob_sha, str)
            or SHA40.fullmatch(item.git_blob_sha) is None
            or not isinstance(item.content, bytes)
        ):
            raise BundleInvalid("snapshot file mode or hash invalid")
        if _git_hash("blob", item.content) != item.git_blob_sha:
            raise BundleInvalid("snapshot blob hash mismatch")
        total += len(item.content)
        if total > MAX_CONTENT_BYTES:
            raise BundleInvalid("snapshot content size limit")
    if _tree_hash(files) != source_tree:
        raise BundleInvalid("snapshot tree hash mismatch")
    return total


def validate_bundle(raw: bytes, expected_tree: str | None = None) -> BundleReadback:
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleInvalid("snapshot bundle size limit")
    try:
        value = json.loads(raw)
        if set(value) != {"schema_version", "kind", "source_tree", "files"}:
            raise BundleInvalid("snapshot bundle fields invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 3:
            raise BundleInvalid("snapshot bundle version invalid")
        if value["kind"] != "github_fixture_snapshot_bundle_v3" or not isinstance(value["files"], list):
            raise BundleInvalid("snapshot bundle kind invalid")
        source_tree = value["source_tree"]
        if not isinstance(source_tree, str) or (expected_tree is not None and source_tree != expected_tree):
            raise BundleInvalid("snapshot tree binding mismatch")
        files: list[BundleFile] = []
        for row in value["files"]:
            if not isinstance(row, dict) or set(row) != {"path", "mode", "git_blob_sha", "content_base64"}:
                raise BundleInvalid("snapshot file fields invalid")
            if not all(isinstance(row[key], str) for key in row):
                raise BundleInvalid("snapshot file value invalid")
            files.append(
                BundleFile(
                    row["path"],
                    row["mode"],
                    row["git_blob_sha"],
                    base64.b64decode(row["content_base64"], validate=True),
                )
            )
        listed = tuple(files)
        total = _validate_files(source_tree, listed)
        if _canonical(listed, source_tree) != raw:
            raise BundleInvalid("snapshot bundle is not canonical")
    except (TypeError, ValueError, UnicodeDecodeError, KeyError) as exc:
        if isinstance(exc, BundleInvalid):
            raise
        raise BundleInvalid("snapshot bundle invalid") from None
    return BundleReadback(source_tree, listed, total, hashlib.sha256(raw).hexdigest(), len(raw))


def read_snapshot(root: Path, relative_path: str, expected_sha256: str, expected_size: int) -> BundleReadback:
    if (
        SHA256.fullmatch(expected_sha256) is None
        or relative_path != f"snapshots/{expected_sha256}.json"
        or not 0 < expected_size <= MAX_BUNDLE_BYTES
        or root.is_symlink()
        or not root.is_dir()
    ):
        raise BundleInvalid("snapshot location invalid")
    directory = root / "snapshots"
    if directory.is_symlink() or not directory.is_dir():
        raise BundleInvalid("snapshot directory invalid")
    descriptor = os.open(directory / f"{expected_sha256}.json", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            raise BundleInvalid("snapshot file metadata mismatch")
        raw = os.read(descriptor, expected_size + 1)
        if len(raw) != expected_size:
            raise BundleInvalid("snapshot file changed during read")
    finally:
        os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise BundleInvalid("snapshot file hash mismatch")
    return validate_bundle(raw)


def write_snapshot(root: Path, raw: bytes) -> BundleReadback:
    readback = validate_bundle(raw)
    if root.is_symlink() or not root.is_dir():
        raise BundleInvalid("snapshot root invalid")
    directory = root / "snapshots"
    if directory.is_symlink() or not directory.is_dir():
        raise BundleInvalid("snapshot directory invalid")
    path = directory / f"{readback.sha256}.json"
    temporary = directory / f".{readback.sha256}.{uuid4()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    observed = read_snapshot(root, f"snapshots/{readback.sha256}.json", readback.sha256, readback.byte_size)
    if observed != readback:
        raise BundleInvalid("snapshot write readback mismatch")
    return readback
