"""Offline staging tests: no provider, account credential, or receipt is involved."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from runner.local_source_stage import (
    StageError,
    StageLimits,
    _admit_repo,
    _AdmittedRepo,
    _parse_tree,
    stage_local_source,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - synthetic test repository only
        ["/usr/bin/git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "root"
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "tracked.txt").write_text("committed\n")
    (repo / "program.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "program.sh").chmod(0o755)
    _git(repo, "add", "tracked.txt", "program.sh")
    _git(repo, "commit", "-qm", "source baseline")
    return (
        root,
        repo,
        {
            "source_id": str(uuid4()),
            "root_id": "workspace",
            "relative_path": "repo",
            "branch": "main",
            "revision": _git(repo, "rev-parse", "HEAD"),
        },
    )


LIMITS = StageLimits(max_files=10, max_bytes=1024, max_tree_bytes=4096, wall_seconds=5)


def test_stages_only_committed_tree_and_returns_exact_tree(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n")
    (repo / "untracked.txt").write_text("not staged\n")
    destination = tmp_path / "checkout"
    result = stage_local_source(source, root, destination, LIMITS)
    assert result.source_revision == source["revision"]
    assert result.source_tree == _git(repo, "rev-parse", "HEAD^{tree}")
    assert result.file_count == 2
    assert result.total_bytes == len(b"committed\n#!/bin/sh\nexit 0\n")
    assert (destination / "tracked.txt").read_text() == "committed\n"
    assert (destination / "program.sh").stat().st_mode & 0o111
    assert not (destination / "untracked.txt").exists()
    assert not (destination / ".git").exists()


def test_branch_or_revision_drift_cannot_stage(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    (repo / "tracked.txt").write_text("next\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "advance")
    with pytest.raises(StageError, match="source_revision_drift"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert not (tmp_path / "checkout").exists()
    source["revision"] = _git(repo, "rev-parse", "HEAD")
    source["branch"] = "other"
    with pytest.raises(StageError, match="source_branch_drift"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)


@pytest.mark.parametrize("path", ["../repo", "/etc", "repo/../other", "repo//x", "repo/.", "repo\\x"])
def test_traversal_is_rejected(tmp_path: Path, path: str) -> None:
    root, _, source = _fixture(tmp_path)
    source["relative_path"] = path
    with pytest.raises(StageError, match="invalid_relative_path"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)


def test_symlink_repo_path_and_destination_are_rejected(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    (root / "link").symlink_to(repo, target_is_directory=True)
    source["relative_path"] = "link"
    with pytest.raises(StageError, match="source_path_unavailable"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    source["relative_path"] = "repo"
    (tmp_path / "checkout").symlink_to(repo, target_is_directory=True)
    with pytest.raises(StageError, match="stage_destination_invalid"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)


def test_symlink_root_component_and_git_storage_are_rejected(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    link_root = tmp_path / "root-link"
    link_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(StageError, match="source_root_unavailable"):
        stage_local_source(source, link_root, tmp_path / "checkout", LIMITS)
    objects = repo / ".git" / "objects"
    objects.rename(repo / ".git" / "objects-held")
    objects.symlink_to(repo / ".git" / "objects-held", target_is_directory=True)
    with pytest.raises(StageError, match="source_repo_invalid"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)


def test_alternates_are_rejected(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    info = repo / ".git" / "objects" / "info"
    info.mkdir(exist_ok=True)
    (info / "alternates").write_text(f"{tmp_path / 'other-objects'}\n")
    with pytest.raises(StageError, match="source_repo_uses_alternates"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)


def test_common_git_directory_redirect_is_rejected(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    (repo / ".git" / "commondir").write_text(f"{tmp_path}\n")
    with pytest.raises(StageError, match="source_repo_invalid"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)


def test_file_byte_and_tree_output_limits(tmp_path: Path) -> None:
    root, _, source = _fixture(tmp_path)
    for limits in (
        StageLimits(1, 1024, 4096, 5),
        StageLimits(10, 1, 4096, 5),
        StageLimits(10, 1024, 1, 5),
    ):
        with pytest.raises(StageError, match="source_tree_limit|source_output_limit"):
            stage_local_source(source, root, tmp_path / "checkout", limits)
        assert not (tmp_path / "checkout").exists()


def test_symlink_blob_and_malformed_tree_entries_are_rejected(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    (repo / "link").symlink_to("tracked.txt")
    _git(repo, "add", "link")
    _git(repo, "commit", "-qm", "add symlink")
    source["revision"] = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(StageError, match="source_tree_unsupported"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert not (tmp_path / "checkout").exists()
    object_id = "a" * 40
    for raw in (
        f"160000 commit {object_id} -\tsubmodule\0".encode(),
        f"100644 blob {object_id} 1\t../escape\0".encode(),
        f"100644 blob {object_id} 1\t.git/config\0".encode(),
        (f"100644 blob {object_id} 1\tfile\0100644 blob {object_id} 1\tfile/child\0").encode(),
        f"100644 blob {object_id} 1\tfile".encode(),
    ):
        with pytest.raises(StageError, match="source_tree_unsupported|source_tree_invalid"):
            _parse_tree(raw, LIMITS)


def test_unavailable_git_object_cleans_partial_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, source = _fixture(tmp_path)
    import runner.local_source_stage as stage_module

    original = stage_module._git_read

    def fail_blob(repo: _AdmittedRepo, args: tuple[str, ...], limit: int, deadline: float) -> bytes:
        if args[0] == "cat-file":
            raise StageError("source_git_failed")
        return original(repo, args, limit, deadline)

    monkeypatch.setattr(stage_module, "_git_read", fail_blob)
    with pytest.raises(StageError, match="source_git_failed"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert not (tmp_path / "checkout").exists()


def test_head_advancing_mid_stage_removes_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo, source = _fixture(tmp_path)
    import runner.local_source_stage as stage_module

    original = stage_module._git_read
    advanced = False

    def advance_after_blob(path: _AdmittedRepo, args: tuple[str, ...], limit: int, deadline: float) -> bytes:
        nonlocal advanced
        output = original(path, args, limit, deadline)
        if args[0] == "cat-file" and not advanced:
            advanced = True
            (repo / "next.txt").write_text("new\n")
            _git(repo, "add", "next.txt")
            _git(repo, "commit", "-qm", "advance during stage")
        return output

    monkeypatch.setattr(stage_module, "_git_read", advance_after_blob)
    with pytest.raises(StageError, match="source_revision_drift"):
        stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert not (tmp_path / "checkout").exists()


def test_empty_tree_and_invalid_limits(tmp_path: Path) -> None:
    root, repo, source = _fixture(tmp_path)
    _git(repo, "rm", "-q", "tracked.txt", "program.sh")
    _git(repo, "commit", "-qm", "empty tree")
    source["revision"] = _git(repo, "rev-parse", "HEAD")
    result = stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert (result.file_count, result.total_bytes) == (0, 0)
    assert list((tmp_path / "checkout").iterdir()) == []
    with pytest.raises(StageError, match="invalid_stage_limits"):
        stage_local_source(source, root, tmp_path / "another", StageLimits(0, 100, 100, 1))


def test_stalled_git_command_is_killed_at_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import runner.local_source_stage as stage_module

    script = tmp_path / "stalled-git"
    script.write_text("#!/bin/sh\nsleep 10\n")
    script.chmod(0o700)
    monkeypatch.setattr(stage_module, "GIT", str(script))
    started = time.monotonic()
    root, _, _ = _fixture(tmp_path)
    with _admit_repo(root, "repo") as admitted:
        with pytest.raises(StageError, match="source_stage_timeout"):
            stage_module._git_read(admitted, ("version",), 10, started + 0.1)
    assert time.monotonic() - started < 2


def test_renamed_repo_and_symlink_replacement_do_not_redirect_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, source = _fixture(tmp_path)
    evil = tmp_path / "evil"
    shutil.copytree(repo, evil)
    (evil / "tracked.txt").write_text("injected\n")
    _git(evil, "add", "tracked.txt")
    _git(evil, "commit", "-qm", "replacement")
    import runner.local_source_stage as stage_module

    original = stage_module._git_read
    swapped = False

    def swap_before_git(admitted: _AdmittedRepo, args: tuple[str, ...], limit: int, deadline: float) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            repo.rename(root / "repo-held")
            repo.symlink_to(evil, target_is_directory=True)
        return original(admitted, args, limit, deadline)

    monkeypatch.setattr(stage_module, "_git_read", swap_before_git)
    result = stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert result.source_revision == source["revision"]
    assert (tmp_path / "checkout" / "tracked.txt").read_text() == "committed\n"


def test_renamed_git_dir_and_symlink_replacement_do_not_redirect_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, repo, source = _fixture(tmp_path)
    evil = tmp_path / "evil"
    shutil.copytree(repo, evil)
    (evil / "tracked.txt").write_text("injected\n")
    _git(evil, "add", "tracked.txt")
    _git(evil, "commit", "-qm", "replacement")
    import runner.local_source_stage as stage_module

    original = stage_module._git_read
    swapped = False

    def swap_before_git(admitted: _AdmittedRepo, args: tuple[str, ...], limit: int, deadline: float) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            (repo / ".git").rename(repo / ".git-held")
            (repo / ".git").symlink_to(evil / ".git", target_is_directory=True)
        return original(admitted, args, limit, deadline)

    monkeypatch.setattr(stage_module, "_git_read", swap_before_git)
    result = stage_local_source(source, root, tmp_path / "checkout", LIMITS)
    assert result.source_revision == source["revision"]
    assert (tmp_path / "checkout" / "tracked.txt").read_text() == "committed\n"
