"""Bounded traversal of exact GitHub Git objects through the existing HTTPS owner."""

from __future__ import annotations

import base64
import binascii
import hashlib
from time import monotonic
from typing import Any
from urllib.parse import quote

from onefoundry.role_repository_file import SHA40, RepositoryReadError, _valid_path
from onefoundry.settings import Settings
from onefoundry.source_github import REPO_PART, GitHubUnavailable, _get


def _sha(value: object) -> str:
    if not isinstance(value, str) or SHA40.fullmatch(value) is None:
        raise RepositoryReadError("GitHub object identity invalid")
    return value


def read_tree(
    owner: str,
    repo: str,
    revision: str,
    path: str,
    credential: str,
    settings: Settings,
    *,
    deadline: float,
) -> list[dict[str, Any]]:
    """Resolve each tree by full object ID; never use a moving branch or recursive API."""
    if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(repo):
        raise RepositoryReadError("GitHub repository invalid")
    if path != "." and not _valid_path(path):
        raise RepositoryReadError("GitHub tree path invalid")
    prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/"

    def get(kind: str, object_id: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RepositoryReadError("repository read timed out", code="source_probe_timed_out")
        try:
            value = _get(
                prefix + kind + "/" + _sha(object_id),
                credential,
                settings,
                timeout_seconds=min(settings.github_timeout_seconds, remaining),
            )
        except GitHubUnavailable:
            raise RepositoryReadError(
                "GitHub read unavailable", code="source_dependency_unavailable"
            ) from None
        if monotonic() > deadline:
            raise RepositoryReadError("repository read timed out", code="source_probe_timed_out")
        if not isinstance(value, dict) or value.get("sha") != object_id:
            raise RepositoryReadError("GitHub object identity changed")
        return value

    commit = get("commits", _sha(revision))
    root = commit.get("tree")
    if not isinstance(root, dict):
        raise RepositoryReadError("GitHub commit tree invalid")
    object_id = _sha(root.get("sha"))
    for part in () if path == "." else path.split("/"):
        tree = get("trees", object_id)
        entries = tree.get("tree")
        if tree.get("truncated") is not False or not isinstance(entries, list):
            raise RepositoryReadError("GitHub tree incomplete")
        matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == part]
        if len(matches) != 1 or matches[0].get("mode") != "040000" or matches[0].get("type") != "tree":
            raise RepositoryReadError("repository directory unavailable")
        object_id = _sha(matches[0].get("sha"))
    tree = get("trees", object_id)
    entries = tree.get("tree")
    if tree.get("truncated") is not False or not isinstance(entries, list):
        raise RepositoryReadError("GitHub tree incomplete")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RepositoryReadError("GitHub tree entry invalid")
        full_path = entry["path"] if path == "." else f"{path}/{entry['path']}"
        if not _valid_path(full_path):
            continue
        mode, kind = entry.get("mode"), entry.get("type")
        if (mode, kind) not in {("040000", "tree"), ("100644", "blob"), ("100755", "blob")}:
            continue
        size = None if kind == "tree" else entry.get("size")
        if kind == "blob" and (type(size) is not int or size < 0):
            raise RepositoryReadError("GitHub tree entry size invalid")
        result.append({"path": full_path, "kind": kind, "sha": _sha(entry.get("sha")), "size": size})
    return result


def read_blob(
    owner: str,
    repo: str,
    revision: str,
    path: str,
    credential: str,
    settings: Settings,
    *,
    limit: int,
    deadline: float,
) -> bytes:
    if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(repo) or not _valid_path(path):
        raise RepositoryReadError("GitHub repository or file path invalid")
    _sha(revision)
    prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/"

    def get(kind: str, object_id: str) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RepositoryReadError("repository read timed out", code="source_probe_timed_out")
        try:
            value = _get(
                prefix + kind + "/" + _sha(object_id),
                credential,
                settings,
                timeout_seconds=min(settings.github_timeout_seconds, remaining),
            )
        except GitHubUnavailable:
            raise RepositoryReadError(
                "GitHub read unavailable", code="source_dependency_unavailable"
            ) from None
        if monotonic() > deadline:
            raise RepositoryReadError("repository read timed out", code="source_probe_timed_out")
        if not isinstance(value, dict) or value.get("sha") != object_id:
            raise RepositoryReadError("GitHub object identity changed")
        return value

    commit = get("commits", revision)
    tree = commit.get("tree")
    if not isinstance(tree, dict):
        raise RepositoryReadError("GitHub commit tree invalid")
    object_id = _sha(tree.get("sha"))
    parts = path.split("/")
    size = 0
    for index, part in enumerate(parts):
        tree = get("trees", object_id)
        entries = tree.get("tree")
        if tree.get("truncated") is not False or not isinstance(entries, list):
            raise RepositoryReadError("GitHub tree incomplete")
        matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == part]
        if len(matches) != 1:
            raise RepositoryReadError("repository file unavailable")
        entry = matches[0]
        object_id = _sha(entry.get("sha"))
        if index < len(parts) - 1:
            if entry.get("mode") != "040000" or entry.get("type") != "tree":
                raise RepositoryReadError("repository directory unsupported")
        else:
            entry_size = entry.get("size")
            if (
                entry.get("mode") not in {"100644", "100755"}
                or entry.get("type") != "blob"
                or type(entry_size) is not int
                or not 0 <= entry_size <= limit
            ):
                raise RepositoryReadError("repository file unsupported or too large")
            size = entry_size
    blob = get("blobs", object_id)
    content = blob.get("content")
    if blob.get("encoding") != "base64" or blob.get("size") != size or not isinstance(content, str):
        raise RepositoryReadError("GitHub blob metadata invalid")
    try:
        data = base64.b64decode(content.replace("\n", ""), validate=True)
    except (ValueError, binascii.Error):
        raise RepositoryReadError("GitHub blob encoding invalid") from None
    actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()  # noqa: S324 - Git identity
    if len(data) != size or actual != object_id:
        raise RepositoryReadError("GitHub blob identity changed")
    return data
