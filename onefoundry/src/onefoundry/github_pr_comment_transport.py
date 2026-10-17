"""Bounded GitHub PR comment HTTP transport; authorization belongs to the gateway."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from onefoundry.github_pr_comment_contract import SavedSource
from onefoundry.settings import Settings
from onefoundry.source_github import SHA, GitHubUnavailable, _get, _NoRedirect


class GitHubCommentTransport:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _base(source: SavedSource) -> str:
        return f"/repos/{quote(source.owner, safe='')}/{quote(source.repo, safe='')}"

    def current_head(self, credential: str, source: SavedSource, number: int) -> str:
        raw = _get(f"{self._base(source)}/pulls/{number}", credential, self.settings)
        if not isinstance(raw, dict):
            raise GitHubUnavailable("GitHub PR metadata invalid")
        base = raw.get("base")
        head = raw.get("head")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        base_sha = base.get("sha") if isinstance(base, dict) else None
        base_ref = base.get("ref") if isinstance(base, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        if (
            raw.get("number") != number
            or raw.get("state") != "open"
            or not isinstance(base_repo, dict)
            or base_repo.get("full_name") != f"{source.owner}/{source.repo}"
            or base_sha != source.saved_revision
            or base_ref != source.branch
            or not isinstance(sha, str)
            or SHA.fullmatch(sha) is None
        ):
            raise GitHubUnavailable("GitHub PR target changed")
        return sha

    def post_comment(self, credential: str, source: SavedSource, number: int, body: str) -> int:
        payload = json.dumps({"body": body}, ensure_ascii=False).encode("utf-8")
        request = Request(  # noqa: S310 - fixed official HTTPS origin and encoded validated path
            self.settings.github_api_url + f"{self._base(source)}/issues/{number}/comments",
            data=payload,
            headers={
                "Authorization": f"Bearer {credential}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "OneFoundry",
            },
            method="POST",
        )
        try:
            with build_opener(ProxyHandler({}), _NoRedirect).open(
                request, timeout=self.settings.github_timeout_seconds
            ) as response:
                if response.status != 201:
                    raise GitHubUnavailable("GitHub comment POST was not acknowledged")
                raw = response.read(self.settings.github_max_response_bytes + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise GitHubUnavailable("GitHub comment POST outcome is ambiguous") from None
        if len(raw) > self.settings.github_max_response_bytes:
            raise GitHubUnavailable("GitHub comment acknowledgment exceeded limit")
        try:
            value: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GitHubUnavailable("GitHub comment acknowledgment invalid") from None
        comment_id = value.get("id") if isinstance(value, dict) else None
        if type(comment_id) is not int or comment_id <= 0 or value.get("body") != body:
            raise GitHubUnavailable("GitHub comment acknowledgment mismatch")
        return comment_id

    def read_comment(self, credential: str, source: SavedSource, number: int, comment_id: int) -> str:
        raw = _get(f"{self._base(source)}/issues/comments/{comment_id}", credential, self.settings)
        if not isinstance(raw, dict) or raw.get("id") != comment_id:
            raise GitHubUnavailable("GitHub comment readback invalid")
        expected_issue = f"{self.settings.github_api_url}{self._base(source)}/issues/{number}"
        body = raw.get("body")
        if (
            raw.get("issue_url") != expected_issue
            or not isinstance(body, str)
            or len(body.encode("utf-8")) > 16_000
        ):
            raise GitHubUnavailable("GitHub comment readback mismatched")
        return body
