"""Bounded read-only requests to the fixed official GitHub API."""

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from onefoundry.settings import Settings

REPO_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


class GitHubUnavailable(Exception):
    """The GitHub account or official API could not be verified."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _get(path: str, credential: str, settings: Settings, *, timeout_seconds: float | None = None) -> Any:
    request = Request(  # noqa: S310 - fixed official HTTPS origin, encoded validated path
        settings.github_api_url + path,
        headers={
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "OneFoundry",
        },
        method="GET",
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect).open(
            request, timeout=settings.github_timeout_seconds if timeout_seconds is None else timeout_seconds
        ) as response:
            if response.status != 200:
                raise GitHubUnavailable("GitHub API unavailable")
            payload = response.read(settings.github_max_response_bytes + 1)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise GitHubUnavailable("GitHub API unavailable or credential rejected") from None
    if len(payload) > settings.github_max_response_bytes:
        raise GitHubUnavailable("GitHub response exceeded limit")
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise GitHubUnavailable("GitHub returned invalid metadata") from None


def list_repositories(credential: str, settings: Settings) -> list[dict[str, Any]]:
    raw = _get("/user/repos?per_page=50&sort=full_name&direction=asc", credential, settings)
    if not isinstance(raw, list):
        raise GitHubUnavailable("GitHub returned invalid repository list")
    output: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise GitHubUnavailable("GitHub returned invalid repository metadata")
        name = item.get("full_name")
        branch = item.get("default_branch")
        owner, separator, repo = name.partition("/") if isinstance(name, str) else ("", "", "")
        if (
            separator != "/"
            or not REPO_PART.fullmatch(owner)
            or not REPO_PART.fullmatch(repo)
            or not isinstance(branch, str)
            or not BRANCH.fullmatch(branch)
            or type(item.get("private")) is not bool
        ):
            raise GitHubUnavailable("GitHub returned invalid repository metadata")
        output.append(
            {
                "full_name": name,
                "default_branch": branch,
                "private": item["private"],
                "html_url": f"https://github.com/{owner}/{repo}",
            }
        )
    return output


def resolve_revision(
    credential: str, settings: Settings, owner: str, repo: str, branch: str | None
) -> tuple[str, str]:
    if not REPO_PART.fullmatch(owner) or not REPO_PART.fullmatch(repo):
        raise ValueError("invalid GitHub repository identifier")
    repository = _get(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}", credential, settings)
    if not isinstance(repository, dict) or repository.get("full_name") != f"{owner}/{repo}":
        raise GitHubUnavailable("GitHub repository metadata did not match")
    selected = branch or repository.get("default_branch")
    if not isinstance(selected, str) or not BRANCH.fullmatch(selected):
        raise GitHubUnavailable("GitHub repository has no valid branch")
    metadata = _get(
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/branches/{quote(selected, safe='')}",
        credential,
        settings,
    )
    if not isinstance(metadata, dict) or metadata.get("name") != selected:
        raise GitHubUnavailable("GitHub branch metadata did not match")
    commit = metadata.get("commit")
    revision = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise GitHubUnavailable("GitHub branch has no valid revision")
    return selected, revision
