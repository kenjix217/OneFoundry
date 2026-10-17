"""Repository source boundaries without a live GitHub credential or database."""

import json
import os
import subprocess
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.settings import Settings
from onefoundry.source_github import GitHubUnavailable, list_repositories, resolve_revision
from onefoundry.source_schemas import GitHubSourceCreate, LocalSourceCreate
from onefoundry.source_store import SourceStore, _git_command, _git_dirty, verify_local


class Response:
    status = 200

    def __init__(self, value: object) -> None:
        self.payload = json.dumps(value).encode()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self.payload


class Opener:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.urls: list[str] = []

    def open(self, request: object, *, timeout: int) -> Response:
        from urllib.request import Request

        assert isinstance(request, Request)
        assert timeout == 5
        self.urls.append(request.full_url)
        return Response(self.values.pop(0))


def run_git(*args: str) -> None:
    subprocess.run(["/usr/bin/git", *args], check=True)  # noqa: S603 - local test fixture only


def test_settings_and_local_root_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(db_password="test-only").local_source_root == "/source-roots/workspace"
    monkeypatch.setenv("LOCAL_SOURCE_ROOT", str(tmp_path))
    assert Settings(db_password="test-only").local_source_root == str(tmp_path)
    disabled = Settings(db_password="test-only", local_source_root="", team_repository_read_enabled=False)
    assert disabled.local_source_root == ""
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", local_source_root="../escape")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", github_api_url="http://127.0.0.1:1234")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", github_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", github_max_response_bytes=1)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", local_source_scan_max_entries=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", local_source_scan_seconds=0)
    assert LocalSourceCreate(root_id="workspace", relative_path=".").relative_path == "."
    engine = create_engine("sqlite://")
    assert SourceStore(engine, "operator", disabled).roots() == []
    with pytest.raises(Unavailable):
        verify_local(disabled, "repo")


@pytest.mark.parametrize("path", ["/etc", "../other", "repo/../other", "repo//x", "repo/.", "repo\\x"])
def test_reject_malformed_local_path(path: str) -> None:
    with pytest.raises(ValidationError):
        LocalSourceCreate(root_id="workspace", relative_path=path)


def test_local_repo_pins_head_and_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    run_git("init", "-q", str(repo))
    run_git("-C", str(repo), "config", "user.name", "Test")
    run_git("-C", str(repo), "config", "user.email", "test@example.com")
    (repo / "file.txt").write_text("content")
    run_git("-C", str(repo), "add", "file.txt")
    run_git("-C", str(repo), "commit", "-qm", "fixture")
    settings = Settings(db_password="test-only", local_source_root=str(root))
    branch, revision = verify_local(settings, "repo")
    assert branch and len(revision) == 40
    command = _git_command(repo.resolve(), root.resolve(), "rev-parse", "HEAD")
    assert command == [
        "/usr/bin/git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repo.resolve()}",
        "-C",
        str(repo.resolve()),
        "rev-parse",
        "HEAD",
    ]
    engine = create_engine("sqlite://")
    found = SourceStore(engine, "operator", settings).discover_local()
    assert found["available"] is True
    assert found["repositories"] == [
        {
            "relative_path": "repo",
            "display_name": "repo",
            "branch": branch,
            "revision": revision,
            "dirty": False,
        }
    ]
    (repo / "file.txt").write_text("changed")
    found = SourceStore(engine, "operator", settings).discover_local()
    assert found["repositories"][0]["dirty"] is True
    marker = tmp_path / "fsmonitor-executed"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    run_git("-C", str(repo), "config", "core.fsmonitor", str(hook))
    found = SourceStore(engine, "operator", settings).discover_local()
    assert found["repositories"][0]["dirty"] is True
    assert not marker.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Conflict):
        verify_local(settings, "link")
    (root / "in-root-link").symlink_to(repo, target_is_directory=True)
    with pytest.raises(Conflict):
        verify_local(settings, "in-root-link")
    with pytest.raises(Conflict):
        _git_command(outside.resolve(), root.resolve(), "rev-parse", "HEAD")
    with pytest.raises(Conflict):
        verify_local(settings, "repo/file.txt")


def test_github_fixed_origin_and_exact_revision() -> None:
    sha = "a" * 40
    opener = Opener(
        [
            {"full_name": "owner/repo", "default_branch": "main"},
            {"name": "main", "commit": {"sha": sha}},
        ]
    )
    settings = Settings(db_password="test-only")
    with patch("onefoundry.source_github.build_opener", return_value=opener):
        assert resolve_revision("secret-token", settings, "owner", "repo", None) == ("main", sha)
    assert opener.urls == [
        "https://api.github.com/repos/owner/repo",
        "https://api.github.com/repos/owner/repo/branches/main",
    ]
    with pytest.raises(ValidationError):
        GitHubSourceCreate(account_id=uuid4(), owner="127.0.0.1:8080", repo="repo")
    with pytest.raises(ValidationError):
        GitHubSourceCreate(account_id=uuid4(), owner="owner", repo="repo", branch="../secret")


def test_dirty_probe_reaps_timed_out_process(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    class HungProcess:
        pid = 123456
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.waits = 0

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            self.returncode = -9
            return -9

        def poll(self) -> int | None:
            return self.returncode

    hung = HungProcess()
    with (
        patch("onefoundry.source_store.subprocess.Popen", return_value=hung) as launch,
        patch("onefoundry.source_store.os.killpg") as kill_group,
    ):
        with pytest.raises(Unavailable):
            _git_dirty(tmp_path, tmp_path, 0.1)
    assert "core.fsmonitor=false" in launch.call_args.args[0]
    assert f"safe.directory={tmp_path}" in launch.call_args.args[0]
    kill_group.assert_called_once_with(hung.pid, 9)
    assert hung.waits == 2
    assert hung.stdout.closed


def test_github_missing_branch_and_bad_response_fail_without_secret_leak() -> None:
    settings = Settings(db_password="test-only")
    opener = Opener([{"full_name": "owner/repo", "default_branch": "main"}, {"name": "other"}])
    with patch("onefoundry.source_github.build_opener", return_value=opener):
        with pytest.raises(GitHubUnavailable) as error:
            resolve_revision("secret-token", settings, "owner", "repo", "main")
    assert "secret-token" not in str(error.value)
    with patch("onefoundry.source_github.build_opener") as factory:
        factory.return_value.open.side_effect = HTTPError(
            "https://api.github.com/user/repos", 401, "secret-token", Message(), None
        )
        with pytest.raises(GitHubUnavailable) as error:
            list_repositories("secret-token", settings)
    assert "secret-token" not in str(error.value)
    opener = Opener([[{"full_name": "owner/repo", "default_branch": "main", "private": True}]])
    with patch("onefoundry.source_github.build_opener", return_value=opener):
        assert list_repositories("secret-token", settings) == [
            {
                "full_name": "owner/repo",
                "default_branch": "main",
                "private": True,
                "html_url": "https://github.com/owner/repo",
            }
        ]
