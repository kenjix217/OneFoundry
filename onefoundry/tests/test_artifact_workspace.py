"""Local artifact bytes are bounded, private, and tied to exact IDs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest

from onefoundry.artifact_workspace import (
    ArtifactAlreadyExists,
    ArtifactCorrupt,
    ArtifactInvalid,
    ArtifactMissing,
    ArtifactWorkspace,
)


class _ReadArguments(TypedDict):
    relative_path: str
    expected_sha256: str
    expected_byte_size: int
    kind: str
    mime_type: str


def _workspace(tmp_path: Path) -> ArtifactWorkspace:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    return ArtifactWorkspace(root)


def test_write_read_exact_identity_and_duplicate(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project, run, artifact = uuid4(), uuid4(), uuid4()
    payload = b"Hello, team.\n"
    saved = workspace.write(project, run, artifact, kind="text", mime_type="text/plain", content=payload)
    assert saved.relative_path == f"{project}/{run}/{artifact}.txt"
    assert saved.sha256 == hashlib.sha256(payload).hexdigest()
    assert saved.byte_size == len(payload)
    assert (
        workspace.read(
            project,
            run,
            artifact,
            relative_path=saved.relative_path,
            expected_sha256=saved.sha256,
            expected_byte_size=saved.byte_size,
            kind="text",
            mime_type="text/plain",
        )
        == payload
    )
    with pytest.raises(ArtifactAlreadyExists):
        workspace.write(project, run, artifact, kind="text", mime_type="text/plain", content=b"other")
    with pytest.raises(ArtifactInvalid):
        workspace.read(
            uuid4(),
            run,
            artifact,
            relative_path=saved.relative_path,
            expected_sha256=saved.sha256,
            expected_byte_size=saved.byte_size,
            kind="text",
            mime_type="text/plain",
        )
    other_run, other = uuid4(), uuid4()
    with pytest.raises(ArtifactMissing):
        workspace.read(
            project,
            other_run,
            other,
            relative_path=f"{project}/{other_run}/{other}.txt",
            expected_sha256=saved.sha256,
            expected_byte_size=saved.byte_size,
            kind="text",
            mime_type="text/plain",
        )


@pytest.mark.parametrize(
    ("kind", "mime_type", "content"),
    [
        ("text", "text/plain", b""),
        ("text", "text/plain", b" \n\t"),
        ("text", "text/plain", b"\xff"),
        ("text", "text/plain", b"a\x00b"),
        ("text", "text/plain", b"password=letmein"),
        ("text", "text/plain; charset=utf-8", b"hello"),
        ("csv", "text/plain", b"a,b"),
        ("html", "text/html", b"<p>x</p>"),
        ("csv", "text/csv", b'name,value\n"hello"," =SUM(1,2)"\n'),
        ("csv", "text/csv", b"name,value\nhello,+123\n"),
        ("csv", "text/csv", b'name,value\n"unterminated'),
    ],
)
def test_invalid_content_and_metadata(tmp_path: Path, kind: str, mime_type: str, content: bytes) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(ArtifactInvalid):
        workspace.write(uuid4(), uuid4(), uuid4(), kind=kind, mime_type=mime_type, content=content)


def test_size_boundary_and_invalid_ids(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project, run = uuid4(), uuid4()
    saved = workspace.write(project, run, uuid4(), kind="text", mime_type="text/plain", content=b"a" * 65536)
    assert saved.byte_size == 65536
    with pytest.raises(ArtifactInvalid):
        workspace.write(project, run, uuid4(), kind="text", mime_type="text/plain", content=b"a" * 65537)
    with pytest.raises(ArtifactInvalid):
        workspace.write("../bad", run, uuid4(), kind="text", mime_type="text/plain", content=b"ok")  # type: ignore[arg-type]


def test_missing_tampered_and_orphaned_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project, run, artifact = uuid4(), uuid4(), uuid4()
    saved = workspace.write(
        project, run, artifact, kind="markdown", mime_type="text/markdown", content=b"# Safe\n"
    )
    arguments: _ReadArguments = {
        "relative_path": saved.relative_path,
        "expected_sha256": saved.sha256,
        "expected_byte_size": saved.byte_size,
        "kind": "markdown",
        "mime_type": "text/markdown",
    }
    target = tmp_path / "workspace" / saved.relative_path
    target.write_bytes(b"# Evil\n")
    with pytest.raises(ArtifactCorrupt):
        workspace.read(project, run, artifact, **arguments)
    target.unlink()
    with pytest.raises(ArtifactMissing):
        workspace.read(project, run, artifact, **arguments)
    # A stranded temporary file never satisfies an artifact read.
    target.with_name(f".{artifact}.abandoned.tmp").write_bytes(b"# Safe\n")
    with pytest.raises(ArtifactMissing):
        workspace.read(project, run, artifact, **arguments)


def test_symlink_hardlink_and_nonprivate_root_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project, run, artifact = uuid4(), uuid4(), uuid4()
    saved = workspace.write(project, run, artifact, kind="text", mime_type="text/plain", content=b"safe")
    arguments: _ReadArguments = {
        "relative_path": saved.relative_path,
        "expected_sha256": saved.sha256,
        "expected_byte_size": saved.byte_size,
        "kind": "text",
        "mime_type": "text/plain",
    }
    target = tmp_path / "workspace" / saved.relative_path
    backup = tmp_path / "backup"
    target.rename(backup)
    target.symlink_to(backup)
    with pytest.raises((ArtifactCorrupt, OSError)):
        workspace.read(project, run, artifact, **arguments)
    target.unlink()
    os.link(backup, target)
    with pytest.raises(ArtifactCorrupt):
        workspace.read(project, run, artifact, **arguments)
    root = tmp_path / "workspace"
    root.chmod(0o755)
    with pytest.raises(ArtifactInvalid):
        workspace.write(uuid4(), uuid4(), uuid4(), kind="text", mime_type="text/plain", content=b"safe")


def test_directory_symlink_and_bad_metadata_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project, run, artifact = uuid4(), uuid4(), uuid4()
    saved = workspace.write(
        project, run, artifact, kind="csv", mime_type="text/csv", content=b"name,value\na,1\n"
    )
    with pytest.raises(ArtifactInvalid):
        workspace.read(
            project,
            run,
            artifact,
            relative_path="../private",
            expected_sha256=saved.sha256,
            expected_byte_size=saved.byte_size,
            kind="csv",
            mime_type="text/csv",
        )
    with pytest.raises(ArtifactInvalid):
        workspace.read(
            project,
            run,
            artifact,
            relative_path=saved.relative_path,
            expected_sha256="bad",
            expected_byte_size=saved.byte_size,
            kind="csv",
            mime_type="text/csv",
        )
    project_dir = tmp_path / "workspace" / str(project)
    backup = tmp_path / "backup-project"
    project_dir.rename(backup)
    project_dir.symlink_to(backup, target_is_directory=True)
    with pytest.raises((ArtifactInvalid, OSError)):
        workspace.read(
            project,
            run,
            artifact,
            relative_path=saved.relative_path,
            expected_sha256=saved.sha256,
            expected_byte_size=saved.byte_size,
            kind="csv",
            mime_type="text/csv",
        )
