"""Bounded tool contracts and authenticated immutable IMAP message references."""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from onefoundry.mail_imap import MailError
from onefoundry.mail_plugin_settings import ascii_text


class MailSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    folder: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=256)
    sender: str = Field(default="", max_length=256)
    subject: str = Field(default="", max_length=256)
    since: str = Field(default="", max_length=10, description="Inclusive YYYY-MM-DD date")
    before: str = Field(default="", max_length=10, description="Exclusive YYYY-MM-DD date")
    before_uid: int | None = Field(default=None, ge=1, le=4294967295)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("folder", "text", "sender", "subject")
    @classmethod
    def safe_query(cls, value: str) -> str:
        return ascii_text(value) if value else value

    @field_validator("since", "before")
    @classmethod
    def iso_date(cls, value: str) -> str:
        if value and date.fromisoformat(value).isoformat() != value:
            raise ValueError("use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def date_order(self) -> "MailSearch":
        if self.since and self.before and self.since >= self.before:
            raise ValueError("before must be later than since")
        return self


class MailRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    message_id: str = Field(min_length=50, max_length=1024, pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$")


class MessageIds:
    """Signed IDs bind UIDVALIDITY+UID to the actual project/account/server/folder."""

    def __init__(self, key: bytes, project_id: UUID, account_id: UUID) -> None:
        if len(key) < 32:
            raise MailError("id_key_invalid")
        self.key = key
        self.project_id = project_id
        self.account_id = account_id

    def _signature(self, payload: str) -> bytes:
        context = f"{self.project_id}:{self.account_id}:{payload}".encode("ascii")
        return hmac.digest(self.key, context, "sha256")

    def encode(self, binding: str, folder: str, validity: int, uid: int) -> str:
        data = json.dumps([1, binding, folder, validity, uid], separators=(",", ":")).encode("ascii")
        payload = base64.urlsafe_b64encode(data).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(self._signature(payload)).decode().rstrip("=")
        return f"{payload}.{signature}"

    def decode(self, token: str, binding: str, folders: Sequence[str]) -> tuple[str, int, int]:
        try:
            MailRead(message_id=token)
            payload, signature = token.split(".")
            expected = base64.urlsafe_b64encode(self._signature(payload)).decode().rstrip("=")
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if (
                not isinstance(data, list)
                or len(data) != 5
                or data[0] != 1
                or data[1] != binding
                or data[2] not in folders
                or type(data[3]) is not int
                or type(data[4]) is not int
                or not 1 <= data[3] <= 4294967295
                or not 1 <= data[4] <= 4294967295
            ):
                raise ValueError
            return str(data[2]), int(data[3]), int(data[4])
        except (ValueError, TypeError, binascii.Error):
            raise MailError("invalid_message_id") from None


def mailbox_binding(host: str, port: int, username: str) -> str:
    return hashlib.sha256(json.dumps([host, port, username], separators=(",", ":")).encode()).hexdigest()
