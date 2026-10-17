"""Explicit, fail-closed grants for the separate memory MCP process."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def printable(value: str) -> str:
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("use a nonempty printable ASCII identifier without whitespace")
    return value


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("use an approved HTTPS URL without credentials, query, or fragment")
    return value


class MemoryGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(pattern=r"^external-memory:[a-zA-Z0-9_-]{1,60}$")
    project_ids: tuple[UUID, ...] = Field(min_length=1, max_length=20)
    company_read: bool = False
    capture: bool = False
    company_capture: bool = False

    _ids = field_validator("subject", "client_id")(printable)

    @model_validator(mode="after")
    def valid_grant(self) -> "MemoryGrant":
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError("project grants must be unique")
        if self.company_capture and (not self.capture or not self.company_read):
            raise ValueError("company capture requires explicit capture and company read grants")
        return self


class MemoryPluginSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_PLUGIN_", env_file=None, extra="forbid")

    public_url: str = Field(max_length=300)
    auth_mode: Literal["oauth", "api_key"] = "oauth"
    api_key_records_path: Path | None = None
    oauth_issuer: str | None = Field(default=None, max_length=300)
    oauth_audience: str | None = Field(default=None, max_length=300)
    oauth_auxiliary_audiences: tuple[str, ...] = Field(default=(), max_length=1)
    oauth_public_key_path: Path | None = None
    grants: tuple[MemoryGrant, ...] = Field(min_length=1, max_length=20)
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(ge=1024, le=65535)
    max_results: int = Field(default=10, ge=1, le=20)
    deadline_seconds: int = Field(default=10, ge=2, le=60)
    max_in_flight: int = Field(default=2, ge=1, le=8)
    max_calls_per_minute: int = Field(default=30, ge=1, le=120)
    max_request_bytes: int = Field(default=131072, ge=4096, le=131072)
    max_token_bytes: int = Field(default=8192, ge=1024, le=16384)
    max_token_seconds: int = Field(default=900, ge=60, le=3600)
    capture_preview_seconds: int = Field(default=300, ge=30, le=900)

    @field_validator("listen_host")
    @classmethod
    def loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1"}:
            raise ValueError("memory MCP must bind only loopback")
        return value

    @field_validator("oauth_issuer", "oauth_audience")
    @classmethod
    def audience(cls, value: str | None) -> str | None:
        return https_url(value) if value is not None else None

    @field_validator("oauth_auxiliary_audiences")
    @classmethod
    def extra_audiences(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(https_url(value) for value in values)

    @field_validator("public_url")
    @classmethod
    def resource(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.path != "/mcp":
            raise ValueError("memory resource must use /mcp")
        if parsed.scheme == "https":
            return https_url(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use HTTPS or the exact same-host HTTP listener for Secure MCP Tunnel")
        return value

    @field_validator("oauth_public_key_path", "api_key_records_path")
    @classmethod
    def absolute_key(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("verification file path must be absolute without parent traversal")
        return value

    @model_validator(mode="after")
    def binding(self) -> "MemoryPluginSettings":
        if self.auth_mode == "oauth":
            if self.oauth_issuer is None or self.oauth_public_key_path is None or self.api_key_records_path:
                raise ValueError("OAuth requires issuer and public key, without API-key records")
        elif (
            self.api_key_records_path is None
            or self.oauth_issuer is not None
            or self.oauth_public_key_path is not None
            or self.oauth_audience is not None
            or self.oauth_auxiliary_audiences
        ):
            raise ValueError("API-key mode requires a records file without OAuth settings")
        parsed = urlsplit(self.public_url)
        if parsed.scheme == "http" and (
            (self.auth_mode == "oauth" and self.oauth_audience is None)
            or parsed.hostname != self.listen_host
            or parsed.port != self.listen_port
        ):
            raise ValueError("tunnel metadata must match the listener and pin its external HTTPS audience")
        if (self.oauth_audience or self.public_url) in self.oauth_auxiliary_audiences:
            raise ValueError("auxiliary audience must differ from the resource audience")
        keys = [(grant.subject, grant.client_id) for grant in self.grants]
        actors = [grant.actor_id for grant in self.grants]
        if len(set(keys)) != len(keys) or len(set(actors)) != len(actors):
            raise ValueError("each subject/client grant must have a unique external actor identity")
        return self
