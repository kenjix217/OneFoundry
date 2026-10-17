"""Durable fake HTTPS source bundle contract and readback."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from onefoundry.github_snapshot_bundle import (
    BundleFile,
    BundleInvalid,
    build_bundle,
    read_snapshot,
    validate_bundle,
    write_snapshot,
)


def git_hash(kind: str, content: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(content)}\0".encode() + content).hexdigest()  # noqa: S324


def sample(path: str = "file.txt", content: bytes = b"hello\n") -> tuple[str, BundleFile]:
    blob = git_hash("blob", content)
    tree = git_hash("tree", f"100644 {path}\0".encode() + bytes.fromhex(blob))
    return tree, BundleFile(path, "100644", blob, content)


def test_bundle_is_canonical_durable_and_restart_safe(tmp_path: Path) -> None:
    root = tmp_path / "source-snapshots"
    (root / "snapshots").mkdir(parents=True)
    tree, file = sample()
    raw = build_bundle(tree, [file])
    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert raw == canonical.encode()
    first = write_snapshot(root, raw)
    assert first.source_tree == tree and first.total_bytes == 6
    path = f"snapshots/{first.sha256}.json"
    # A crashed writer's temporary file cannot block a later write of the same digest.
    (root / "snapshots" / f".{first.sha256}.retained.tmp").write_bytes(b"stale")
    assert write_snapshot(root, raw) == first
    assert read_snapshot(root, path, first.sha256, first.byte_size) == first
    assert (root / "snapshots" / f".{first.sha256}.retained.tmp").read_bytes() == b"stale"


@pytest.mark.parametrize("path", ["../x", ".git/config", "a/../b", "a\\b", "e\u0301", "\ud800"])
def test_unsafe_paths_rejected(path: str) -> None:
    tree, _ = sample()
    with pytest.raises(BundleInvalid):
        build_bundle(tree, [BundleFile(path, "100644", git_hash("blob", b"x"), b"x")])


def test_case_aliases_and_malformed_modes_rejected() -> None:
    tree, file = sample()
    with pytest.raises(BundleInvalid, match="path collision"):
        build_bundle(tree, [file, BundleFile("FILE.txt", file.mode, file.git_blob_sha, file.content)])
    with pytest.raises(BundleInvalid, match="path collision"):
        build_bundle(
            tree,
            [
                BundleFile("A/x", file.mode, file.git_blob_sha, file.content),
                BundleFile("a/y", file.mode, file.git_blob_sha, file.content),
            ],
        )
    with pytest.raises(BundleInvalid, match="mode or hash"):
        build_bundle(tree, [BundleFile(file.path, ["100644"], file.git_blob_sha, file.content)])  # type: ignore[arg-type]


def test_tampered_and_noncanonical_bundle_rejected(tmp_path: Path) -> None:
    root = tmp_path / "source-snapshots"
    (root / "snapshots").mkdir(parents=True)
    tree, file = sample()
    raw = build_bundle(tree, [file])
    value = json.loads(raw)
    value["files"][0]["content_base64"] = base64.b64encode(b"wrong").decode()
    with pytest.raises(BundleInvalid, match="blob hash"):
        validate_bundle(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(BundleInvalid, match="not canonical"):
        validate_bundle(json.dumps(json.loads(raw)).encode())
    saved = write_snapshot(root, raw)
    stored = root / "snapshots" / f"{saved.sha256}.json"
    stored.write_bytes(raw[:-1] + b" ")
    with pytest.raises(BundleInvalid, match="file hash"):
        read_snapshot(root, f"snapshots/{saved.sha256}.json", saved.sha256, saved.byte_size)
    stored.unlink()
    stored.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        read_snapshot(root, f"snapshots/{saved.sha256}.json", saved.sha256, saved.byte_size)
