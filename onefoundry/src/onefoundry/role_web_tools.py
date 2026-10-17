"""Typed, bounded public-web contracts for admitted Researcher role calls.

The Tool Gateway owns run admission and receipts. PublicWebClient owns network
destination checks and response limits; this module validates the model-facing
arguments and result shape without granting a tool to any profile.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator

from onefoundry.workflow_schemas import SECRET_PATTERN, StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.public_web_client import PublicWebClient
    from onefoundry.settings import Settings


SEARCH_TOOL_ID = "public_web_search_v1"
FETCH_TOOL_ID = "public_page_fetch_v1"

SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 120}},
    "required": ["query"],
    "additionalProperties": False,
}
FETCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"url": {"type": "string", "maxLength": 2048, "format": "uri"}},
    "required": ["url"],
    "additionalProperties": False,
}

_URL_IN_QUERY = re.compile(
    r"(?:\b(?:https?://|www\.)\S+|(?:^|\s)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:\b|/))",
    re.IGNORECASE,
)
_EMAIL_IN_QUERY = re.compile(r"\b[^\s@]+@[^\s@]+\.[A-Za-z]{2,}\b")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$", re.IGNORECASE)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".invalid")
_SENSITIVE_QUERY_KEYS = (
    "token",
    "key",
    "password",
    "secret",
    "credential",
    "session",
    "authorization",
    "auth",
    "code",
)


def _public_url(value: str) -> str:
    if len(value.encode("utf-8")) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("public URL exceeds its bound or contains whitespace")
    if "\\" in value or SECRET_PATTERN.search(value):
        raise ValueError("public URL contains invalid or sensitive text")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("public URL is invalid") from None
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in (None, 443)
    ):
        raise ValueError("public URL must use HTTPS without credentials or fragments")
    host = parts.hostname or ""
    if (
        not _HOST.fullmatch(host)
        or "." not in host
        or host.startswith(".")
        or host.endswith(".")
        or ".." in host
        or host.endswith(_PRIVATE_SUFFIXES)
    ):
        raise ValueError("public URL host is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("public URL must use a public DNS hostname")
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        normalized = "".join(char for char in key.casefold() if char.isalnum())
        if any(marker in normalized for marker in _SENSITIVE_QUERY_KEYS):
            raise ValueError("public URL contains a sensitive query field")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("source date must be UTC")
    return value.astimezone(UTC)


class SearchArguments(StrictModel):
    query: str = Field(min_length=1, max_length=120, strict=True)

    @field_validator("query")
    @classmethod
    def public_query(cls, value: str) -> str:
        query = local_text(value)
        if len(query.encode("utf-8")) > 120:
            raise ValueError("search query exceeds its byte limit")
        if _URL_IN_QUERY.search(query) or _EMAIL_IN_QUERY.search(query):
            raise ValueError("search query must contain public terms, not a URL or email")
        return query


class FetchArguments(StrictModel):
    url: str = Field(min_length=1, max_length=2048, strict=True)

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return _public_url(value)


class SearchItem(StrictModel):
    url: str = Field(min_length=1, max_length=2048, strict=True)
    title: str = Field(max_length=512, strict=True)
    snippet: str = Field(max_length=2000, strict=True)
    published_at: datetime | None

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return _public_url(value)

    @field_validator("published_at")
    @classmethod
    def utc_published(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class SearchOutput(StrictModel):
    results: list[SearchItem] = Field(max_length=20)
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def utc_fetched(cls, value: datetime) -> datetime:
        return _utc(value)


class FetchOutput(StrictModel):
    url: str = Field(min_length=1, max_length=2048, strict=True)
    title: str = Field(max_length=512, strict=True)
    text: str = Field(max_length=16000, strict=True)
    published_at: datetime | None
    fetched_at: datetime

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return _public_url(value)

    @field_validator("published_at", "fetched_at")
    @classmethod
    def utc_date(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


def parse_search(arguments: object) -> dict[str, Any]:
    return SearchArguments.model_validate(arguments).model_dump(mode="json")


def parse_fetch(arguments: object) -> dict[str, Any]:
    return FetchArguments.model_validate(arguments).model_dump(mode="json")


def validate_search(result: dict[str, Any]) -> None:
    SearchOutput.model_validate(result)


def validate_fetch(result: dict[str, Any]) -> None:
    FetchOutput.model_validate(result)


def execute_search(client: PublicWebClient, settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    """Ask the configured public search owner for bounded, citeable results."""
    return client.search(
        query=arguments["query"],
        endpoint=settings.public_web_search_endpoint,
        max_results=settings.public_web_max_results,
        timeout_seconds=settings.public_web_timeout_seconds,
        max_response_bytes=settings.public_web_max_response_bytes,
    )


def execute_fetch(client: PublicWebClient, settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch one already validated public HTTPS page with bounded text output."""
    return client.fetch(
        url=arguments["url"],
        timeout_seconds=settings.public_web_timeout_seconds,
        max_response_bytes=settings.public_web_max_response_bytes,
        max_text_chars=settings.public_web_max_text_chars,
    )


@dataclass(frozen=True, slots=True)
class WebTool:
    # Run snapshots pin IDs, so changed semantics must use a new identifier.
    tool_id: str
    version: int
    description: str
    parameters: dict[str, Any]
    parse_arguments: Callable[[object], dict[str, Any]]
    execute: Callable[[PublicWebClient, Settings, dict[str, Any]], dict[str, Any]]
    validate_output: Callable[[dict[str, Any]], None]


WEB_TOOLS: dict[str, WebTool] = {
    SEARCH_TOOL_ID: WebTool(
        tool_id=SEARCH_TOOL_ID,
        version=1,
        description="Search using public words without URLs, domains or email addresses. "
        "Return bounded source snippets with URLs and dates.",
        parameters=SEARCH_PARAMETERS,
        parse_arguments=parse_search,
        execute=execute_search,
        validate_output=validate_search,
    ),
    FETCH_TOOL_ID: WebTool(
        tool_id=FETCH_TOOL_ID,
        version=1,
        description="Fetch bounded text from a public HTTPS URL returned by public_web_search_v1 "
        "in this same run, with its source URL and date.",
        parameters=FETCH_PARAMETERS,
        parse_arguments=parse_fetch,
        execute=execute_fetch,
        validate_output=validate_fetch,
    ),
}
