"""Exact pinned Git staging proof uses only a synthetic local remote."""

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

import runner.github_fixture_stage as stage_module
from runner.github_fixture_stage import stage_pinned_fixture
from runner.local_source_stage import StageError, StageLimits, StageResult

LIMITS = StageLimits(10, 4096, 8192, 8)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - local synthetic repository only
        ["/usr/bin/git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.invalid")
    (work / "safe.txt").write_text("saved\n")
    git(work, "add", "safe.txt")
    git(work, "commit", "-qm", "first")
    revision = git(work, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(remote)], check=True)  # noqa: S603
    git(work, "push", "-q", str(remote), "main:main")
    return work, remote, revision


def stage(tmp_path: Path, remote: Path, revision: str, limits: StageLimits = LIMITS) -> StageResult:
    return stage_pinned_fixture(
        remote=remote,
        project_id=uuid4(),
        source_id=uuid4(),
        account_id=uuid4(),
        owner="example",
        repo_name="repo",
        branch="main",
        saved_revision=revision,
        destination=tmp_path / "stage",
        limits=limits,
        max_git_bytes=1_000_000,
    )


def test_exact_pinned_fixture_tree_and_no_remote_metadata(tmp_path: Path) -> None:
    work, remote, revision = fixture(tmp_path)
    (work / "safe.txt").write_text("dirty\n")
    result = stage(tmp_path, remote, revision)
    assert result.source_revision == revision
    assert (tmp_path / "stage" / "safe.txt").read_text() == "saved\n"
    assert not (tmp_path / "stage" / ".git").exists()
    assert not (tmp_path / "stage" / "remote.git").exists()
    assert list(tmp_path.glob("onefoundry-github-fixture-*")) == []


def test_branch_drift_rejected_before_fetch(tmp_path: Path) -> None:
    work, remote, revision = fixture(tmp_path)
    (work / "safe.txt").write_text("next\n")
    git(work, "add", "safe.txt")
    git(work, "commit", "-qm", "next")
    git(work, "push", "-q", str(remote), "main:main")
    with pytest.raises(StageError, match="source_revision_drift"):
        stage(tmp_path, remote, revision)
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("mode", ["symlink", "gitlink"])
def test_unsafe_tree_rejected(tmp_path: Path, mode: str) -> None:
    work, remote, _ = fixture(tmp_path)
    if mode == "symlink":
        (work / "unsafe").symlink_to("/etc/passwd")
        git(work, "add", "unsafe")
    else:
        git(work, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",nested")
    git(work, "commit", "-qm", mode)
    git(work, "push", "-q", str(remote), "main:main")
    with pytest.raises(StageError, match="source_tree_(unsupported|invalid)"):
        stage(tmp_path, remote, git(work, "rev-parse", "HEAD"))
    assert not (tmp_path / "stage").exists()


def test_size_limit_rejects_without_partial_stage(tmp_path: Path) -> None:
    work, remote, _ = fixture(tmp_path)
    (work / "large.txt").write_bytes(b"x" * 100)
    git(work, "add", "large.txt")
    git(work, "commit", "-qm", "larger")
    git(work, "push", "-q", str(remote), "main:main")
    with pytest.raises(StageError, match="source_tree_limit"):
        stage(tmp_path, remote, git(work, "rev-parse", "HEAD"), StageLimits(10, 10, 8192, 8))
    assert not (tmp_path / "stage").exists()


def test_post_stage_verification_failure_removes_disposable_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, remote, revision = fixture(tmp_path)
    check = stage_module._git_bytes
    calls = 0

    def fail_second_check(git_dir: Path, maximum: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StageError("source_fetch_limit")
        check(git_dir, maximum)

    monkeypatch.setattr(stage_module, "_git_bytes", fail_second_check)
    with pytest.raises(StageError, match="source_fetch_limit"):
        stage(tmp_path, remote, revision)
    assert calls == 2
    assert not (tmp_path / "stage").exists()


def test_different_owner_remote_uses_only_private_exact_fixture_config(tmp_path: Path) -> None:
    work, remote, revision = fixture(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
    }
    without = subprocess.run(  # noqa: S603 - controlled local Git ownership regression
        ["/usr/bin/git", "-C", str(work), "ls-remote", "--heads", str(remote)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert without.returncode != 0
    assert "dubious ownership" in without.stderr

    safe_config = tmp_path / "fixture-gitconfig"
    stage_module._fixture_git_config(safe_config, remote)
    assert safe_config.stat().st_mode & 0o777 == 0o600
    configured = subprocess.run(  # noqa: S603 - reads only the private fixture config
        ["/usr/bin/git", "config", "--file", str(safe_config), "--get-all", "safe.directory"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert configured.stdout.strip() == str(remote)

    with_exact = subprocess.run(  # noqa: S603 - exercises Git's local transport child
        ["/usr/bin/git", "-C", str(work), "ls-remote", "--heads", str(remote)],
        env={**stage_module._git_env(safe_config), "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert with_exact.stdout.strip() == f"{revision}\trefs/heads/main"
