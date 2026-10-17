"""Bounded IMAP IDLE notifications; the existing poll owner still ingests mail.

Python 3.12 has no public IDLE API. This narrow compatibility adapter uses
imaplib's command framing and tests it against a TLS server. Timed-out streams
are discarded rather than reused. No message body, model, or write command is used.
"""

from __future__ import annotations

import imaplib
import threading
from typing import Any

from onefoundry.mail_imap import ImapMailbox, MailError


class IdleWait:
    def __init__(self, mailbox: ImapMailbox, seconds: float) -> None:
        self.mailbox = mailbox
        self.seconds = seconds
        self._guard = threading.Lock()
        self._client: Any = None
        self._cancelled = False
        self.entered = False

    def cancel(self) -> None:
        with self._guard:
            self._cancelled = True
            client = self._client
        if client is not None:
            try:
                client.sock.shutdown(2)
            except OSError:
                pass

    def wait(self) -> bool:
        client = self.mailbox._open()
        with self._guard:
            self._client = client
            cancelled = self._cancelled
        try:
            if cancelled:
                return False
            self.mailbox._examine(client)
            if "IDLE" not in {str(item).upper() for item in client.capabilities}:
                return False
            tag = client._new_tag()
            client.send(tag + b" IDLE\r\n")
            client.sock.settimeout(self.seconds)
            if not client._get_line().startswith(b"+"):
                raise MailError("idle_rejected")
            self.entered = True
            # EXISTS/RECENT coalesces to one ordinary poll. Flag-only notices
            # also cause a poll, which de-duplicates by UID as usual.
            notification = client._get_line()
            if not notification.startswith(b"*") or notification.startswith(b"* BYE"):
                raise MailError("idle_connection_closed")
            client.sock.settimeout(self.mailbox.config.timeout_seconds)
            client.send(b"DONE\r\n")
            status, _ = client._get_tagged_response(tag)
            if status != "OK":
                raise MailError("idle_rejected")
            return True
        except TimeoutError:
            return False
        except (imaplib.IMAP4.error, OSError):
            if self._cancelled:
                return False
            raise MailError("idle_connection_failed") from None
        finally:
            # A timeout leaves imaplib's buffered stream unusable; close it
            # directly without reading a LOGOUT response.
            with self._guard:
                self._client = None
            try:
                client.shutdown()
            except OSError:
                pass
