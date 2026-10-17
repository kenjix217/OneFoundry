"""Read-only IMAP access to one mailbox folder.

The folder is opened with EXAMINE, and bodies are fetched with BODY.PEEK, so
reading never changes flags such as Seen. Only implicit TLS is supported.
"""

from __future__ import annotations

import email
import imaplib
import re
import ssl
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr
from html.parser import HTMLParser

SIZE = re.compile(rb"RFC822\.SIZE (\d+)")


class MailError(Exception):
    """A mailbox operation failed with a fixed, secret-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class MailConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    folder: str
    timeout_seconds: float
    max_message_bytes: int


@dataclass(frozen=True, slots=True)
class Probe:
    capabilities: tuple[str, ...]
    idle: bool
    uidvalidity: int
    uidnext: int | None
    exists: int


@dataclass(frozen=True, slots=True)
class Fetched:
    uid: int
    size: int
    raw: bytes
    truncated: bool


@dataclass(frozen=True, slots=True)
class Batch:
    uidvalidity: int
    messages: tuple[Fetched, ...]
    highest_uid: int


@dataclass(frozen=True, slots=True)
class ReadSession:
    client: imaplib.IMAP4_SSL
    uidvalidity: int
    uidnext: int | None
    exists: int


@dataclass(frozen=True, slots=True)
class Parsed:
    from_address: str
    from_domain: str
    subject: str
    message_id: str
    in_reply_to: str
    date: str
    text: str
    references: tuple[str, ...] = ()


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _text(html: str) -> str:
    parser = _TextOnly()
    parser.feed(html)
    return " ".join(parser.parts)


def parse_message(raw: bytes, max_text_chars: int = 4000) -> Parsed:
    message = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(message, EmailMessage)
    address = parseaddr(str(message.get("From", "")))[1].strip().lower()
    body = message.get_body(preferencelist=("plain", "html"))
    content = ""
    if body is not None:
        try:
            value = body.get_content()
        except (LookupError, ValueError):
            value = ""
        content = _text(value) if body.get_content_subtype() == "html" else str(value)
    return Parsed(
        from_address=address[:320],
        from_domain=address.rpartition("@")[2][:255] if "@" in address else "",
        subject=" ".join(str(message.get("Subject", "")).split())[:300],
        message_id=str(message.get("Message-ID", "")).strip()[:300],
        in_reply_to=str(message.get("In-Reply-To", "")).strip()[:300],
        date=str(message.get("Date", "")).strip()[:100],
        text=" ".join(content.split())[:max_text_chars],
        references=tuple(re.findall(r"<[^<>\s]{1,298}>", str(message.get("References", ""))[:10000])[-30:]),
    )


def _ok(result: tuple[str, Sequence[object]], code: str) -> Sequence[object]:
    status, data = result
    if status != "OK":
        raise MailError(code)
    return data


class ImapMailbox:
    def __init__(self, config: MailConfig, context: ssl.SSLContext | None = None) -> None:
        self.config = config
        self.context = context or ssl.create_default_context()

    def _open(self, factory: Callable[..., imaplib.IMAP4_SSL] | None = None) -> imaplib.IMAP4_SSL:
        try:
            client = (factory or imaplib.IMAP4_SSL)(
                self.config.host,
                self.config.port,
                ssl_context=self.context,
                timeout=self.config.timeout_seconds,
            )
        except ssl.SSLError:
            raise MailError("tls_failed") from None
        except TimeoutError:
            raise MailError("timeout") from None
        except imaplib.IMAP4.error:
            raise MailError("imap_protocol_failed") from None
        except OSError:
            raise MailError("connection_failed") from None
        try:
            client.login(self.config.username, self.config.password)
        except BaseException as failure:
            self._close(client)
            if isinstance(failure, imaplib.IMAP4.error):
                raise MailError("authentication_failed") from None
            if isinstance(failure, TimeoutError):
                raise MailError("timeout") from None
            if isinstance(failure, OSError):
                raise MailError("connection_failed") from None
            raise
        return client

    @staticmethod
    def _close(client: imaplib.IMAP4_SSL) -> None:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError, MailError):
            pass
        finally:
            try:
                client.shutdown()
            except OSError:
                pass

    def _examine(self, client: imaplib.IMAP4_SSL) -> tuple[int, int | None, int]:
        try:
            data = _ok(client.select(self._quoted_folder(), readonly=True), "folder_unavailable")
            validity = client.response("UIDVALIDITY")[1]
            uidnext = client.response("UIDNEXT")[1]
        except imaplib.IMAP4.error:
            raise MailError("folder_unavailable") from None
        if not validity or validity[0] is None:
            raise MailError("uidvalidity_missing")
        exists = int(data[0]) if data and isinstance(data[0], bytes) and data[0].isdigit() else 0
        next_uid = int(uidnext[0]) if uidnext and isinstance(uidnext[0], bytes) else None
        return int(validity[0]), next_uid, exists

    def _quoted_folder(self) -> str:
        folder = self.config.folder.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{folder}"'

    @contextmanager
    def read_session(self, factory: Callable[..., imaplib.IMAP4_SSL] | None = None) -> Iterator[ReadSession]:
        """Open a fresh TLS/EXAMINE session for bounded read-only adapters."""
        client = self._open(factory)
        try:
            validity, next_uid, exists = self._examine(client)
            if not 1 <= validity <= 4294967295 or next_uid is None or not 1 <= next_uid <= 4294967295:
                raise MailError("mailbox_identity_invalid")
            yield ReadSession(client, validity, next_uid, exists)
        except TimeoutError:
            raise MailError("timeout") from None
        except (ValueError, TypeError, OverflowError, imaplib.IMAP4.error):
            raise MailError("imap_protocol_failed") from None
        except OSError:
            raise MailError("connection_failed") from None
        finally:
            self._close(client)

    def probe(self) -> Probe:
        client = self._open()
        try:
            capabilities = tuple(str(item).upper() for item in client.capabilities)
            uidvalidity, uidnext, exists = self._examine(client)
            return Probe(capabilities, "IDLE" in capabilities, uidvalidity, uidnext, exists)
        except OSError:
            raise MailError("connection_failed") from None
        finally:
            self._close(client)

    def fetch_since(self, last_uid: int, limit: int) -> Batch:
        """Fetch up to ``limit`` messages with UID above ``last_uid``, oldest first."""
        client = self._open()
        try:
            uidvalidity, _, _ = self._examine(client)
            found = _ok(client.uid("SEARCH", f"UID {last_uid + 1}:*"), "search_failed")
            listed = found[0] if found and isinstance(found[0], bytes) else b""
            uids = sorted(
                uid for uid in (int(item) for item in listed.split() if item.isdigit()) if uid > last_uid
            )
            highest = max(uids, default=last_uid)
            messages: list[Fetched] = []
            for uid in uids[:limit]:
                size_data = _ok(client.uid("FETCH", str(uid), "(RFC822.SIZE)"), "fetch_failed")
                size_match = SIZE.search(b" ".join(item for item in size_data if isinstance(item, bytes)))
                size = int(size_match.group(1)) if size_match else 0
                limit_bytes = self.config.max_message_bytes
                body_data = _ok(
                    client.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{limit_bytes}>)"), "fetch_failed"
                )
                raw = b"".join(
                    part[1] for part in body_data if isinstance(part, tuple) and isinstance(part[1], bytes)
                )
                messages.append(Fetched(uid, size, raw[:limit_bytes], size > limit_bytes))
            return Batch(uidvalidity, tuple(messages), messages[-1].uid if len(uids) > limit else highest)
        except (imaplib.IMAP4.error, OSError):
            raise MailError("fetch_failed") from None
        finally:
            self._close(client)
