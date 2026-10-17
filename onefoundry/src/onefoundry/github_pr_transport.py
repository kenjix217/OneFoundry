"""Bounded PR metadata and patch read from the fixed official GitHub API.

This transport does not authorize egress. Its caller must admit the project,
saved source, credential, and one network attempt before invoking it.
"""

from __future__ import annotations

from time import monotonic
from typing import Any
from urllib.parse import quote

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.settings import Settings
from onefoundry.source_github import BRANCH, REPO_PART, SHA, _get
from onefoundry.source_github import GitHubUnavailable as GitHubUnavailable

MAX_FILES = 10
MAX_PATCH_BYTES = 2048
MAX_PATCH_SOURCE_BYTES = 8192
MAX_BODY_BYTES = 4096
MAX_TITLE_BYTES = 256
MAX_PATH_BYTES = 512
MAX_CHANGED_FILES = 3000
MAX_CHANGE_COUNT = 1_000_000
FILE_STATUSES = frozenset({"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"})
RESULT_KEYS = frozenset(
    {"number", "title", "body", "state", "head_sha", "base_sha", "files", "files_truncated"}
)
FILE_KEYS = frozenset({"path", "status", "additions", "deletions", "patch_excerpt", "patch_truncated"})


def _bounded_get(path: str, credential: str, settings: Settings, deadline: float | None) -> Any:
    if deadline is None:
        return _get(path, credential, settings)
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise GitHubUnavailable("GitHub PR read deadline exceeded")
    return _get(path, credential, settings, timeout_seconds=min(settings.github_timeout_seconds, remaining))


def _text(value: object, maximum: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise ValueError("invalid GitHub PR text")
    if (
        contains_sensitive_text(value)
        or any(
            ord(char) < 32 and char not in ("\n", "\r", "\t") if multiline else ord(char) < 32
            for char in value
        )
        or "\x7f" in value
    ):
        raise ValueError("unsafe GitHub PR text")
    return value


def _count(value: object, maximum: int = MAX_CHANGE_COUNT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid GitHub PR count")
    return value


def _path(value: object) -> str:
    path = _text(value, MAX_PATH_BYTES)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "://" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ValueError("invalid GitHub PR file path")
    return path


def _metadata(
    raw: object,
    owner: str,
    repo: str,
    number: int,
    expected_base_sha: str,
    expected_branch: str | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid GitHub PR metadata")
    base = raw.get("base")
    head = raw.get("head")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    base_sha = base.get("sha") if isinstance(base, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if (
        type(raw.get("number")) is not int
        or raw["number"] != number
        or not isinstance(base_repo, dict)
        or base_repo.get("full_name") != f"{owner}/{repo}"
        or base_sha != expected_base_sha
        or (expected_branch is not None and base_ref != expected_branch)
        or not isinstance(head_sha, str)
        or SHA.fullmatch(head_sha) is None
        or raw.get("state") not in ("open", "closed")
    ):
        raise ValueError("GitHub PR revision or repository changed")
    title = _text(raw.get("title"), MAX_TITLE_BYTES)
    if not title.strip():
        raise ValueError("invalid GitHub PR title")
    raw_body = raw.get("body")
    body = _text("" if raw_body is None else raw_body, MAX_BODY_BYTES, multiline=True)
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": raw["state"],
        "head_sha": head_sha,
        "base_sha": base_sha,
        "changed_files": _count(raw.get("changed_files"), MAX_CHANGED_FILES),
    }


def _file(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid GitHub PR file metadata")
    path = _path(raw.get("filename"))
    status = raw.get("status")
    if not isinstance(status, str) or status not in FILE_STATUSES:
        raise ValueError("invalid GitHub PR file status")
    patch = _text(raw.get("patch"), MAX_PATCH_SOURCE_BYTES, multiline=True)
    if not patch or "GIT binary patch" in patch or "Binary files " in patch or "\x00" in patch:
        raise ValueError("binary or absent GitHub PR patch")
    encoded = patch.encode("utf-8")
    excerpt = encoded[:MAX_PATCH_BYTES].decode("utf-8", errors="ignore")
    return {
        "path": path,
        "status": status,
        "additions": _count(raw.get("additions")),
        "deletions": _count(raw.get("deletions")),
        "patch_excerpt": excerpt,
        "patch_truncated": len(encoded) > MAX_PATCH_BYTES,
    }


def validate_pull_request(result: dict[str, Any]) -> None:
    """Reject any field expansion or unsafe result before model/tool exposure."""
    if not isinstance(result, dict) or result.keys() != RESULT_KEYS:
        raise ValueError("invalid GitHub PR result")
    number = result["number"]
    if type(number) is not int or not 1 <= number <= 2_147_483_647:
        raise ValueError("invalid GitHub PR number")
    if not _text(result["title"], MAX_TITLE_BYTES).strip():
        raise ValueError("invalid GitHub PR title")
    _text(result["body"], MAX_BODY_BYTES, multiline=True)
    if result["state"] not in ("open", "closed"):
        raise ValueError("invalid GitHub PR state")
    for key in ("head_sha", "base_sha"):
        value = result[key]
        if not isinstance(value, str) or SHA.fullmatch(value) is None:
            raise ValueError("invalid GitHub PR revision")
    if type(result["files_truncated"]) is not bool or not isinstance(result["files"], list):
        raise ValueError("invalid GitHub PR files")
    if len(result["files"]) > MAX_FILES:
        raise ValueError("GitHub PR file limit exceeded")
    seen: set[str] = set()
    for file in result["files"]:
        if not isinstance(file, dict) or file.keys() != FILE_KEYS:
            raise ValueError("invalid GitHub PR file")
        path = _path(file["path"])
        if path in seen or not isinstance(file["status"], str) or file["status"] not in FILE_STATUSES:
            raise ValueError("invalid GitHub PR file")
        seen.add(path)
        _count(file["additions"])
        _count(file["deletions"])
        _text(file["patch_excerpt"], MAX_PATCH_BYTES, multiline=True)
        if type(file["patch_truncated"]) is not bool:
            raise ValueError("invalid GitHub PR patch truncation")


def read_pull_request(
    credential: str,
    settings: Settings,
    owner: str,
    repo: str,
    number: int,
    expected_base_sha: str,
    expected_branch: str | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Read one PR and a single bounded file page, rejecting detected revision drift."""
    if (
        not isinstance(credential, str)
        or not credential
        or not isinstance(owner, str)
        or REPO_PART.fullmatch(owner) is None
        or not isinstance(repo, str)
        or REPO_PART.fullmatch(repo) is None
        or type(number) is not int
        or not 1 <= number <= 2_147_483_647
        or not isinstance(expected_base_sha, str)
        or SHA.fullmatch(expected_base_sha) is None
        or (
            expected_branch is not None
            and (not isinstance(expected_branch, str) or BRANCH.fullmatch(expected_branch) is None)
        )
    ):
        raise ValueError("invalid GitHub PR request")
    prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}"
    try:
        first = _metadata(
            _bounded_get(prefix, credential, settings, deadline),
            owner,
            repo,
            number,
            expected_base_sha,
            expected_branch,
        )
        raw_files = _bounded_get(
            f"{prefix}/files?per_page={MAX_FILES + 1}&page=1", credential, settings, deadline
        )
        if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES + 1:
            raise ValueError("invalid GitHub PR files response")
        files = [_file(item) for item in raw_files[:MAX_FILES]]
        if len({item["path"] for item in files}) != len(files):
            raise ValueError("duplicate GitHub PR file")
        last = _metadata(
            _bounded_get(prefix, credential, settings, deadline),
            owner,
            repo,
            number,
            expected_base_sha,
            expected_branch,
        )
        if first != last or len(raw_files) > first["changed_files"]:
            raise ValueError("GitHub PR changed during read")
        result: dict[str, Any] = {
            "number": first["number"],
            "title": first["title"],
            "body": first["body"],
            "state": first["state"],
            "head_sha": first["head_sha"],
            "base_sha": first["base_sha"],
            "files_truncated": first["changed_files"] > len(files),
            "files": files,
        }
        validate_pull_request(result)
        return result
    except (ValueError, TypeError, KeyError):
        raise GitHubUnavailable("GitHub PR metadata unavailable or changed") from None
