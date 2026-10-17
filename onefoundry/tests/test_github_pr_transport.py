"""Bounded GitHub PR transport tests with an injected, network-free API read."""

from time import monotonic
from typing import Any

import pytest

from onefoundry import github_pr_transport as transport
from onefoundry.settings import Settings

BASE = "a" * 40
HEAD = "b" * 40


def _settings() -> Settings:
    return Settings(db_password="test-only")


def _pull(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "number": 7,
        "title": "Repair calculator",
        "body": "Adds the missing increment.",
        "state": "open",
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": "acme/app"}},
        "head": {"sha": HEAD},
        "changed_files": 1,
    }
    value.update(changes)
    return value


def _files(**changes: Any) -> list[dict[str, Any]]:
    value: dict[str, Any] = {
        "filename": "calculator.py",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": "@@ -1 +1 @@\n-return value\n+return value + 1\n",
    }
    value.update(changes)
    return [value]


def test_read_binds_saved_base_and_returns_only_bounded_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def get(path: str, credential: str, settings: Settings) -> Any:
        assert credential == "test-credential"
        assert settings.github_api_url == "https://api.github.com"
        calls.append(path)
        if len(calls) in (1, 3):
            return _pull(html_url="https://attacker.example/path", merged=False)
        return _files(raw_url="https://attacker.example/raw")

    monkeypatch.setattr(transport, "_get", get)
    result = transport.read_pull_request("test-credential", _settings(), "acme", "app", 7, BASE, "main")
    assert calls == [
        "/repos/acme/app/pulls/7",
        f"/repos/acme/app/pulls/7/files?per_page={transport.MAX_FILES + 1}&page=1",
        "/repos/acme/app/pulls/7",
    ]
    assert result == {
        "number": 7,
        "title": "Repair calculator",
        "body": "Adds the missing increment.",
        "state": "open",
        "head_sha": HEAD,
        "base_sha": BASE,
        "files_truncated": False,
        "files": [
            {
                "path": "calculator.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch_excerpt": "@@ -1 +1 @@\n-return value\n+return value + 1\n",
                "patch_truncated": False,
            }
        ],
    }
    transport.validate_pull_request(result)
    assert "attacker.example" not in str(result)
    assert "test-credential" not in str(result)


@pytest.mark.parametrize(
    ("owner", "repo", "number", "base"),
    [
        ("../bad", "app", 7, BASE),
        ("acme", "bad/repo", 7, BASE),
        ("acme", "app", True, BASE),
        ("acme", "app", 0, BASE),
        ("acme", "app", 7, "not-a-sha"),
    ],
)
def test_invalid_request_never_calls_api(
    monkeypatch: pytest.MonkeyPatch, owner: str, repo: str, number: int, base: str
) -> None:
    monkeypatch.setattr(transport, "_get", lambda *_: pytest.fail("network attempted"))
    with pytest.raises(ValueError):
        transport.read_pull_request("test-credential", _settings(), owner, repo, number, base)


@pytest.mark.parametrize(
    "changes",
    [
        {"number": 8},
        {"base": {"sha": BASE, "repo": {"full_name": "other/app"}}},
        {"base": {"sha": HEAD, "repo": {"full_name": "acme/app"}}},
        {"head": {"sha": "invalid"}},
        {"title": "password=supersecret"},
        {"body": "Bearer abcdefghijklmnop"},
        {"changed_files": -1},
    ],
)
def test_mismatched_or_unsafe_metadata_fails_before_files_request(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    calls = 0

    def get(*_: Any) -> Any:
        nonlocal calls
        calls += 1
        return _pull(**changes)

    monkeypatch.setattr(transport, "_get", get)
    with pytest.raises(transport.GitHubUnavailable):
        transport.read_pull_request("test-credential", _settings(), "acme", "app", 7, BASE)
    assert calls == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"patch": None},
        {"patch": "GIT binary patch\n"},
        {"patch": "token=abcdef"},
        {"filename": "../escape.py"},
        {"filename": "secret=hidden"},
        {"additions": -1},
        {"patch": "x" * 10000},
    ],
)
def test_binary_secret_oversize_or_unsafe_file_rejected(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    responses = iter((_pull(), _files(**changes)))
    monkeypatch.setattr(transport, "_get", lambda *_: next(responses))
    with pytest.raises(transport.GitHubUnavailable):
        transport.read_pull_request("test-credential", _settings(), "acme", "app", 7, BASE)


def test_file_list_and_patch_excerpts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    patch = "@@ -1 +1 @@\n" + ("+long text\n" * 300)
    files = _files(patch=patch)
    files += [dict(files[0], filename=f"file-{index}.py") for index in range(1, transport.MAX_FILES + 1)]
    responses = iter((_pull(changed_files=len(files)), files, _pull(changed_files=len(files))))
    monkeypatch.setattr(transport, "_get", lambda *_: next(responses))
    result = transport.read_pull_request("test-credential", _settings(), "acme", "app", 7, BASE)
    assert len(result["files"]) == transport.MAX_FILES
    assert result["files_truncated"] is True
    assert len(result["files"][0]["patch_excerpt"].encode()) <= transport.MAX_PATCH_BYTES
    assert result["files"][0]["patch_truncated"] is True


def test_postread_rejects_head_movement(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter((_pull(), _files(), _pull(head={"sha": "c" * 40})))
    monkeypatch.setattr(transport, "_get", lambda *_: next(responses))
    with pytest.raises(transport.GitHubUnavailable):
        transport.read_pull_request("test-credential", _settings(), "acme", "app", 7, BASE)


def test_approved_branch_is_bound_even_when_base_sha_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport,
        "_get",
        lambda *_: _pull(
            base={
                "sha": BASE,
                "ref": "release",
                "repo": {"full_name": "acme/app"},
            }
        ),
    )
    with pytest.raises(transport.GitHubUnavailable):
        transport.read_pull_request("test-credential", _settings(), "acme", "app", 7, BASE, "main")


def test_deadline_bounds_each_request_and_expired_deadline_never_calls_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter((_pull(), _files(), _pull()))
    timeouts: list[float] = []

    def get(_path: str, _credential: str, _settings: Settings, *, timeout_seconds: float) -> Any:
        timeouts.append(timeout_seconds)
        return next(responses)

    monkeypatch.setattr(transport, "_get", get)
    result = transport.read_pull_request(
        "test-credential", _settings(), "acme", "app", 7, BASE, "main", monotonic() + 1
    )
    assert result["number"] == 7
    assert len(timeouts) == 3 and all(0 < value <= 1 for value in timeouts)
    with pytest.raises(transport.GitHubUnavailable, match="deadline"):
        transport.read_pull_request(
            "test-credential", _settings(), "acme", "app", 7, BASE, "main", monotonic() - 1
        )
    assert len(timeouts) == 3


def test_result_validator_rejects_extra_fields_and_secrets() -> None:
    good = {
        "number": 7,
        "title": "Repair",
        "body": "Safe",
        "state": "open",
        "head_sha": HEAD,
        "base_sha": BASE,
        "files_truncated": False,
        "files": [],
    }
    transport.validate_pull_request(good)
    with pytest.raises(ValueError):
        transport.validate_pull_request({**good, "credential": "hidden"})
    with pytest.raises(ValueError):
        transport.validate_pull_request({**good, "body": "api_key=sensitive"})
