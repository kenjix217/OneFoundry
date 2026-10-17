"""Read-only mailboxes through the Gmail API and Microsoft Graph.

Both adapters return original MIME bytes so parsing matches IMAP mail. Cursors are
provider change tokens: a Gmail history ID or a Graph delta link. Stored Graph
links are accepted only on the fixed Graph origin. Nothing is modified or sent.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.request import Request

from onefoundry.inference.contracts import TransportError
from onefoundry.inference.http import HTTPTransport, send_http
from onefoundry.mail_imap import MailError

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0"
TokenSource = Callable[[], str]


@dataclass(frozen=True, slots=True)
class ApiMessage:
    message_id: str
    raw: bytes
    size: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ApiBatch:
    messages: tuple[ApiMessage, ...]
    cursor: str
    skipped: int = 0


class _Client:
    def __init__(self, token: TokenSource, http: HTTPTransport, timeout: float, max_bytes: int) -> None:
        self.token = token
        self.http = http
        self.timeout = timeout
        self.max_bytes = max_bytes

    def get(self, url: str, *, raw: bool = False) -> tuple[int, Any]:
        request = Request(  # noqa: S310 - callers build URLs on fixed provider origins
            url,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "Accept": "*/*" if raw else "application/json",
            },
        )
        try:
            result = self.http(request, timeout=self.timeout)
        except TransportError:
            raise MailError("connection_failed") from None
        if result.status in {401, 403}:
            raise MailError("authentication_failed")
        if raw or result.status >= 300:
            return result.status, result.body
        try:
            return result.status, json.loads(result.body)
        except (ValueError, UnicodeDecodeError):
            raise MailError("fetch_failed") from None


class GmailMailbox(_Client):
    def baseline(self) -> str:
        status, data = self.get(f"{GMAIL}/profile")
        if status != 200 or not isinstance(data, dict) or not str(data.get("historyId", "")).isdigit():
            raise MailError("folder_unavailable")
        return str(data["historyId"])

    def fetch(self, cursor: str, limit: int) -> ApiBatch:
        if not cursor.isdigit():
            raise MailError("cursor_expired")
        ids: list[str] = []
        latest = cursor
        page = ""
        while True:
            url = (
                f"{GMAIL}/history?startHistoryId={cursor}&historyTypes=messageAdded&labelId=INBOX&maxResults=100"
                + (f"&pageToken={quote(page)}" if page else "")
            )
            status, data = self.get(url)
            if status == 404:
                raise MailError("cursor_expired")
            if status != 200 or not isinstance(data, dict):
                raise MailError("fetch_failed")
            latest = str(data.get("historyId", latest))
            for entry in data.get("history", []) or []:
                for added in entry.get("messagesAdded", []) or []:
                    message = added.get("message") or {}
                    if isinstance(message.get("id"), str) and message["id"] not in ids:
                        ids.append(message["id"])
            page = str(data.get("nextPageToken") or "")
            if not page or len(ids) >= 500:
                break
        messages: list[ApiMessage] = []
        for message_id in ids[-limit:]:
            status, data = self.get(f"{GMAIL}/messages/{quote(message_id, safe='')}?format=raw")
            if status == 404:
                continue
            if status != 200 or not isinstance(data, dict) or not isinstance(data.get("raw"), str):
                raise MailError("fetch_failed")
            body = base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))
            messages.append(
                ApiMessage(message_id, body[: self.max_bytes], len(body), len(body) > self.max_bytes)
            )
        # Gmail history cannot resume mid-page, so a burst above the limit keeps the newest
        # messages and reports how many older ones were skipped.
        return ApiBatch(tuple(messages), latest, max(len(ids) - limit, 0))


class GraphMailbox(_Client):
    MAX_BASELINE_PAGES = 400

    def baseline(self) -> str:
        """Return a delta link positioned after the current inbox, importing nothing.

        ``$deltatoken=latest`` is used when Graph honours it; otherwise the ID-only delta
        pages are walked to the end (bounded) and their items are discarded.
        """
        url = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$select=id&$deltatoken=latest"
        for _ in range(self.MAX_BASELINE_PAGES):
            status, data = self.get(url)
            if status != 200 or not isinstance(data, dict):
                raise MailError("folder_unavailable")
            link, follow = data.get("@odata.deltaLink"), data.get("@odata.nextLink")
            if isinstance(link, str) and link.startswith(GRAPH + "/"):
                return link
            if not isinstance(follow, str) or not follow.startswith(GRAPH + "/"):
                raise MailError("folder_unavailable")
            url = follow
        raise MailError("mailbox_too_large_for_baseline")

    def fetch(self, cursor: str, limit: int) -> ApiBatch:
        if not cursor.startswith(GRAPH + "/"):
            raise MailError("cursor_expired")
        ids: list[str] = []
        url = cursor
        next_cursor = cursor
        while True:
            status, data = self.get(url)
            if status in {404, 410}:
                raise MailError("cursor_expired")
            if status != 200 or not isinstance(data, dict):
                raise MailError("fetch_failed")
            for item in data.get("value", []) or []:
                if isinstance(item, dict) and "@removed" not in item and isinstance(item.get("id"), str):
                    ids.append(item["id"])
            follow = data.get("@odata.nextLink")
            done = data.get("@odata.deltaLink")
            if isinstance(follow, str) and follow.startswith(GRAPH + "/") and len(ids) < limit:
                url = follow
                continue
            if isinstance(follow, str) and follow.startswith(GRAPH + "/"):
                next_cursor = follow
            elif isinstance(done, str) and done.startswith(GRAPH + "/"):
                next_cursor = done
            break
        messages: list[ApiMessage] = []
        for message_id in ids[:limit]:
            status, body = self.get(f"{GRAPH}/me/messages/{quote(message_id, safe='')}/$value", raw=True)
            if status == 404:
                continue
            if status != 200 or not isinstance(body, bytes):
                raise MailError("fetch_failed")
            messages.append(
                ApiMessage(message_id, body[: self.max_bytes], len(body), len(body) > self.max_bytes)
            )
        return ApiBatch(tuple(messages), next_cursor)


def mailbox(
    protocol: str, token: TokenSource, *, http: HTTPTransport = send_http, timeout: float, max_bytes: int
) -> GmailMailbox | GraphMailbox:
    if protocol == "gmail":
        return GmailMailbox(token, http, timeout, max_bytes)
    if protocol == "graph":
        return GraphMailbox(token, http, timeout, max_bytes)
    raise MailError("protocol_unsupported")
