"""Bounded IMAP search/read over the existing TLS/EXAMINE session owner."""

from __future__ import annotations

import imaplib
import re
import ssl
import time
from collections.abc import Buffer, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from io import BufferedReader
from typing import cast

from onefoundry.mail_imap import ImapMailbox, MailConfig, MailError, parse_message
from onefoundry.mail_plugin_contracts import MailSearch, MessageIds, mailbox_binding
from onefoundry.mail_plugin_settings import MailPluginSettings

_UID = re.compile(rb"\bUID (\d+)\b")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)\b")
_BODY = re.compile(rb"\bBODY\[.*?\]<0> \{(\d+)\}")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class _BoundedClient(imaplib.IMAP4_SSL):
    """Bound literal/line allocation and absolute elapsed time, including LOGIN."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        timeout: float,
        deadline: float,
        max_bytes: int,
        max_line_bytes: int,
        max_session_bytes: int,
        clock: Callable[[], float],
    ) -> None:
        self._deadline = deadline
        self._clock = clock
        self._max_bytes = max_bytes
        self._max_line_bytes = max_line_bytes
        self._budget = max_session_bytes
        remaining = deadline - clock()
        if remaining <= 0:
            raise MailError("timeout")
        try:
            super().__init__(host, port, ssl_context=ssl_context, timeout=min(timeout, remaining))
        except BaseException:
            if hasattr(self, "sock"):
                self.shutdown()
            raise

    def _remaining(self) -> None:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise MailError("timeout")
        self.sock.settimeout(min(float(self.sock.gettimeout() or remaining), remaining))

    def read(self, size: int) -> bytes:
        if size < 0 or size > self._max_bytes or size > self._budget:
            raise MailError("imap_response_too_large")
        data = bytearray()
        while len(data) < size:
            self._remaining()
            chunk = cast(BufferedReader, self.file).read1(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
            self._budget -= len(chunk)
        return bytes(data)

    def readline(self) -> bytes:
        data = bytearray()
        while True:
            if len(data) >= self._max_line_bytes or self._budget <= 0:
                raise MailError("imap_response_too_large")
            self._remaining()
            char = self.file.read(1)
            if not char:
                break
            self._budget -= 1
            data.extend(char)
            if char == b"\n":
                break
        return bytes(data)

    def send(self, data: Buffer) -> None:
        self._remaining()
        super().send(data)


@dataclass(frozen=True)
class MailSummary:
    message_id: str
    folder: str
    sender: str
    subject: str
    date: str
    size_bytes: int
    headers_truncated: bool


@dataclass(frozen=True)
class SearchResult:
    messages: tuple[MailSummary, ...]
    uid_start: int
    uid_end: int
    older_window_before_uid: int | None
    more_matches_in_window: bool
    next_before_uid: int | None = None
    untrusted_email_content: bool = True


@dataclass(frozen=True)
class ReadResult:
    message_id: str
    folder: str
    sender: str
    subject: str
    date: str
    text: str
    size_bytes: int
    truncated: bool
    attachments_included: bool = False
    untrusted_email_content: bool = True


def _ok(result: tuple[str, Sequence[object]]) -> Sequence[object]:
    if result[0] != "OK":
        raise MailError("imap_command_failed")
    return result[1]


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _criteria(query: MailSearch, start: int, end: int) -> str:
    parts = [f"UID {start}:{end}"]
    for key, value in [("TEXT", query.text), ("FROM", query.sender), ("SUBJECT", query.subject)]:
        if value:
            parts.append(f"{key} {_quoted(value)}")
    for key, value in [("SINCE", query.since), ("BEFORE", query.before)]:
        if value:
            day = date.fromisoformat(value)
            parts.append(f"{key} {day.day:02d}-{_MONTHS[day.month - 1]}-{day.year:04d}")
    return " ".join(parts)


def _size(client: imaplib.IMAP4_SSL, uid: int) -> int:
    data = _ok(client.uid("FETCH", str(uid), "(UID RFC822.SIZE)"))
    if data == [None]:
        raise MailError("message_unavailable")
    lines = [item for item in data if isinstance(item, bytes)]
    if len(lines) != 1:
        raise MailError("imap_protocol_failed")
    found_uid, size = _UID.search(lines[0]), _SIZE.search(lines[0])
    if found_uid is None or int(found_uid[1]) != uid or size is None:
        raise MailError("imap_protocol_failed")
    return int(size[1])


def _fetch(client: imaplib.IMAP4_SSL, uid: int, section: str, limit: int) -> bytes:
    data = _ok(client.uid("FETCH", str(uid), f"(UID BODY.PEEK[{section}]<0.{limit}>)"))
    bodies = [item for item in data if isinstance(item, tuple)]
    if not bodies and data == [None]:
        raise MailError("message_unavailable")
    if len(bodies) != 1 or len(bodies[0]) != 2:
        raise MailError("imap_protocol_failed")
    header, raw = bodies[0]
    if not isinstance(header, bytes) or not isinstance(raw, bytes):
        raise MailError("imap_protocol_failed")
    found_uid, body = _UID.search(header), _BODY.search(header)
    if (
        found_uid is None
        or int(found_uid[1]) != uid
        or body is None
        or f"BODY[{section}]<0> ".encode() not in header
        or int(body[1]) != len(raw)
        or len(raw) > limit
    ):
        raise MailError("imap_protocol_failed")
    return raw


class SearchMailbox:
    def __init__(
        self,
        config: MailConfig,
        policy: MailPluginSettings,
        ids: MessageIds,
        *,
        context: ssl.SSLContext | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config, self.policy, self.ids = config, policy, ids
        self.mailbox = ImapMailbox(config, context)
        self.clock = clock
        self.binding = mailbox_binding(config.host, config.port, config.username)

    def _factory(self) -> Callable[..., imaplib.IMAP4_SSL]:
        # SEARCH is confined to this many UIDs, so its response line is bounded too.
        return partial(
            _BoundedClient,
            deadline=self.clock() + self.policy.deadline_seconds,
            max_bytes=max(self.config.max_message_bytes, self.policy.header_bytes),
            max_line_bytes=max(self.policy.header_bytes, self.policy.uid_window * 11 + 512),
            max_session_bytes=self.policy.max_session_bytes,
            clock=self.clock,
        )

    def search(self, query: MailSearch) -> SearchResult:
        with self.mailbox.read_session(self._factory()) as session:
            assert session.uidnext is not None
            end = min(session.uidnext - 1, (query.before_uid or session.uidnext) - 1)
            start = max(1, end - self.policy.uid_window + 1)
            if session.exists == 0 or end < start:
                return SearchResult((), start, end, None, False)
            data = _ok(session.client.uid("SEARCH", _criteria(query, start, end)))
            if len(data) != 1 or not isinstance(data[0], bytes):
                raise MailError("imap_protocol_failed")
            parts = data[0].split()
            if len(parts) > self.policy.uid_window or any(not part.isdigit() for part in parts):
                raise MailError("imap_protocol_failed")
            uids = sorted({int(part) for part in parts}, reverse=True)
            if any(not start <= uid <= end for uid in uids):
                raise MailError("imap_protocol_failed")
            messages: list[MailSummary] = []
            limit = min(query.limit, self.policy.max_results)
            for uid in uids[:limit]:
                size = _size(session.client, uid)
                raw = _fetch(
                    session.client,
                    uid,
                    "HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)",
                    self.policy.header_bytes,
                )
                parsed = parse_message(raw)
                messages.append(
                    MailSummary(
                        self.ids.encode(self.binding, query.folder, session.uidvalidity, uid),
                        query.folder,
                        parsed.from_address,
                        parsed.subject,
                        parsed.date,
                        size,
                        len(raw) >= self.policy.header_bytes,
                    )
                )
            more = len(uids) > limit
            next_uid = uids[limit - 1] if more else start if start > 1 else None
            return SearchResult(tuple(messages), start, end, start if start > 1 else None, more, next_uid)

    def read(self, message_id: str, expected_validity: int, uid: int) -> ReadResult:
        with self.mailbox.read_session(self._factory()) as session:
            if session.uidvalidity != expected_validity:
                raise MailError("stale_message_id")
            size = _size(session.client, uid)
            raw = _fetch(session.client, uid, "", self.config.max_message_bytes)
            parsed = parse_message(raw, self.policy.body_chars)
            return ReadResult(
                message_id,
                self.config.folder,
                parsed.from_address,
                parsed.subject,
                parsed.date,
                parsed.text,
                size,
                size > len(raw) or len(parsed.text) >= self.policy.body_chars,
            )
