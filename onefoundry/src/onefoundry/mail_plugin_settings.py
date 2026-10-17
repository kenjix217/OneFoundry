"""Fail-closed configuration for the separate personal mail MCP process."""

from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from onefoundry.mail_schemas import HOST


def ascii_text(value: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("use nonempty printable ASCII without control characters")
    return value


class MailPluginSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAIL_PLUGIN_", env_file=None, extra="forbid")

    project_id: UUID
    account_id: UUID
    username: str = Field(min_length=3, max_length=320)
    imap_host: str = Field(min_length=1, max_length=253)
    imap_port: int = Field(ge=1, le=65535)
    folders: tuple[str, ...] = Field(min_length=1, max_length=20)
    actor_id: str = Field(min_length=1, max_length=80)
    id_key_id: UUID
    public_url: str = Field(max_length=300)
    oauth_audience: str | None = Field(default=None, max_length=300)
    oauth_auxiliary_audiences: tuple[str, ...] = Field(default=(), max_length=1)
    oauth_issuer: str = Field(max_length=300)
    oauth_public_key_path: Path
    oauth_subjects: tuple[str, ...] = Field(min_length=1, max_length=10)
    oauth_clients: tuple[str, ...] = Field(min_length=1, max_length=10)
    oauth_scope: str = Field(default="mail.read", min_length=1, max_length=80)
    # Binding is loopback-only; an approved tunnel or TLS proxy owns remote access.
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(ge=1024, le=65535)
    max_results: int = Field(default=10, ge=1, le=20)
    uid_window: int = Field(default=2000, ge=1, le=10000)
    header_bytes: int = Field(default=8192, ge=1024, le=32768)
    body_chars: int = Field(default=4000, ge=100, le=10000)
    deadline_seconds: int = Field(default=60, ge=2, le=120)
    max_calls_per_minute: int = Field(default=30, ge=1, le=120)
    max_request_bytes: int = Field(default=16384, ge=1024, le=65536)
    max_token_bytes: int = Field(default=8192, ge=1024, le=16384)
    max_session_bytes: int = Field(default=2097152, ge=65536, le=4194304)
    max_token_seconds: int = Field(default=900, ge=60, le=3600)

    @field_validator("listen_host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1"}:
            raise ValueError("mail MCP must bind a loopback address")
        return value

    @field_validator("imap_host")
    @classmethod
    def server_name(cls, value: str) -> str:
        if not HOST.fullmatch(value) or value != value.lower():
            raise ValueError("use the exact lowercase verified IMAP server name")
        return value

    @field_validator("username", "actor_id", "oauth_scope")
    @classmethod
    def safe_scalar(cls, value: str) -> str:
        return ascii_text(value)

    @field_validator("folders", "oauth_subjects", "oauth_clients")
    @classmethod
    def safe_allowlist(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(len(value) > 200 for value in values):
            raise ValueError("allowlist entries must be unique and at most 200 characters")
        return tuple(ascii_text(value) for value in values)

    @field_validator("oauth_issuer")
    @classmethod
    def https_only(cls, value: str) -> str:
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

    @field_validator("oauth_audience")
    @classmethod
    def audience_url(cls, value: str | None) -> str | None:
        return cls.https_only(value) if value is not None else None

    @field_validator("oauth_auxiliary_audiences")
    @classmethod
    def auxiliary_audience_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(cls.https_only(value) for value in values)

    @field_validator("public_url")
    @classmethod
    def metadata_url(cls, value: str) -> str:
        if value.startswith("https://"):
            return cls.https_only(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use HTTPS or the exact HTTP loopback listener for an approved tunnel")
        return value

    @model_validator(mode="after")
    def tunnel_binding(self) -> "MailPluginSettings":
        parsed = urlsplit(self.public_url)
        if parsed.scheme == "http" and (
            self.oauth_audience is None
            or parsed.hostname != self.listen_host
            or parsed.port != self.listen_port
        ):
            raise ValueError("HTTP metadata requires the exact loopback listener and an HTTPS token audience")
        if (self.oauth_audience or self.public_url) in self.oauth_auxiliary_audiences:
            raise ValueError("auxiliary audience must differ from the required MCP audience")
        return self

    @field_validator("public_url")
    @classmethod
    def mcp_path(cls, value: str) -> str:
        if urlsplit(value).path != "/mcp":
            raise ValueError("public URL must use the /mcp path")
        return value

    @field_validator("oauth_public_key_path")
    @classmethod
    def absolute_key_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("OAuth verification key path must be absolute")
        return value
