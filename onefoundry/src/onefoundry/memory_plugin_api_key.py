"""Scoped revocable bearer keys; the service stores only SHA-256 digests."""

import hashlib
import hmac
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Literal

from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from onefoundry.memory_plugin_auth import granted
from onefoundry.memory_plugin_settings import MemoryPluginSettings


class MemoryApiKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    expires_at: int = Field(gt=0)
    scopes: list[Literal["memory.read", "memory.capture"]] = Field(default=["memory.read"])

    @field_validator("scopes")
    @classmethod
    def bounded_scopes(cls, values: list[str]) -> list[str]:
        if "memory.read" not in values or len(set(values)) != len(values):
            raise ValueError("key scopes require memory.read and unique approved capabilities")
        return values


def load_key_records(path: Path) -> list[MemoryApiKeyRecord]:
    """Read each time: withdrawing a digest takes effect on the next request."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("key records must be an owned private regular file")
        raw = os.read(fd, 16385)
    finally:
        os.close(fd)
    if len(raw) > 16384:
        raise ValueError("key records exceed the bounded file size")
    records = TypeAdapter(list[MemoryApiKeyRecord]).validate_python(json.loads(raw))
    if len(records) > 20 or len({item.sha256 for item in records}) != len(records):
        raise ValueError("key records must contain at most twenty unique digests")
    return records


class MemoryApiKeyVerifier:
    def __init__(self, policy: MemoryPluginSettings) -> None:
        if policy.auth_mode != "api_key" or policy.api_key_records_path is None:
            raise ValueError("API-key verification requires an explicit key policy")
        self.policy, self.path = policy, policy.api_key_records_path
        self._records()  # Invalid or missing files fail startup, never downgrade authentication.

    def _records(self) -> list[MemoryApiKeyRecord]:
        records = load_key_records(self.path)
        if any(granted(self.policy, item.subject, item.client_id) is None for item in records):
            raise ValueError("each key must bind an existing server-owned actor grant")
        for item in records:
            grant = granted(self.policy, item.subject, item.client_id)
            if "memory.capture" in item.scopes and (grant is None or not grant.capture):
                raise ValueError("capture key scopes require an explicitly approved capture grant")
        return records

    async def verify_token(self, token: str) -> AccessToken | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        try:
            match = next((item for item in self._records() if hmac.compare_digest(item.sha256, digest)), None)
        except (OSError, ValueError):
            return None
        if match is None or match.expires_at <= int(time.time()):
            return None
        return AccessToken(
            token=token,
            client_id=match.client_id,
            subject=match.subject,
            scopes=list(match.scopes),
            expires_at=match.expires_at,
            resource=self.policy.public_url,
        )
