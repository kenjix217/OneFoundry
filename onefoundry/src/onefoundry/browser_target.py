"""Exact local browser target policy; this module never launches a browser."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID


class BrowserTargetError(ValueError):
    """A requested or observed browser target violates its approval binding."""


@dataclass(frozen=True)
class BrowserTargetLimits:
    max_url_chars: int
    max_viewport_width: int
    max_viewport_height: int
    max_page_bytes: int
    max_timeout_seconds: int
    max_lifetime_seconds: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (
                self.max_url_chars,
                self.max_viewport_width,
                self.max_viewport_height,
                self.max_page_bytes,
                self.max_timeout_seconds,
                self.max_lifetime_seconds,
            )
        ):
            raise BrowserTargetError("browser target limits must be positive integers")


def _parse_exact_local_url(url: str, max_chars: int) -> tuple[str, str]:
    if not isinstance(url, str) or not url or len(url) > max_chars:
        raise BrowserTargetError("invalid browser target URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserTargetError("invalid browser target URL") from exc
    if parsed.scheme != "http" or port is None or not 1 <= port <= 65535:
        raise BrowserTargetError("browser target must use a local HTTP origin with an explicit port")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserTargetError("browser target URL credentials are forbidden")
    hostname = parsed.hostname
    host = {"localhost": "localhost", "127.0.0.1": "127.0.0.1", "::1": "[::1]"}.get(hostname or "")
    if host is None or parsed.netloc != f"{host}:{port}":
        raise BrowserTargetError("browser target must use a canonical loopback host")
    path = parsed.path
    if (
        not path.startswith("/")
        or re.fullmatch(r"/[A-Za-z0-9._~/-]*", path) is None
        or "//" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        raise BrowserTargetError("browser target path is malformed or unsafe")
    origin = f"http://{host}:{port}"
    if url != origin + path:
        raise BrowserTargetError("browser target URL is not canonical")
    return origin, path


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BrowserTargetError(f"{name} must be timezone aware")
    return value.astimezone(UTC)


def _bounded(value: int, maximum: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise BrowserTargetError(f"{name} is outside approved bounds")


@dataclass(frozen=True)
class BrowserTarget:
    project_id: UUID
    run_id: UUID
    actor_id: str
    approved_url: str
    expires_at: datetime
    idempotency_key: UUID
    viewport_width: int
    viewport_height: int
    max_page_bytes: int
    timeout_seconds: int
    limits: BrowserTargetLimits
    issued_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("project_id", self.project_id),
            ("run_id", self.run_id),
            ("idempotency_key", self.idempotency_key),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise BrowserTargetError(f"{name} must be a nonzero UUID")
        if (
            not isinstance(self.actor_id, str)
            or not 1 <= len(self.actor_id) <= 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_./-]*", self.actor_id) is None
        ):
            raise BrowserTargetError("actor_id is invalid")
        if not isinstance(self.limits, BrowserTargetLimits):
            raise BrowserTargetError("browser target limits are required")
        _parse_exact_local_url(self.approved_url, self.limits.max_url_chars)
        remaining = _aware_utc(self.expires_at, "expires_at") - _aware_utc(self.issued_at, "issued_at")
        if not 0 < remaining.total_seconds() <= self.limits.max_lifetime_seconds:
            raise BrowserTargetError("browser target is expired or exceeds approved lifetime")
        _bounded(self.viewport_width, self.limits.max_viewport_width, "viewport_width")
        _bounded(self.viewport_height, self.limits.max_viewport_height, "viewport_height")
        _bounded(self.max_page_bytes, self.limits.max_page_bytes, "max_page_bytes")
        _bounded(self.timeout_seconds, self.limits.max_timeout_seconds, "timeout_seconds")

    @classmethod
    def bind(
        cls,
        *,
        project_id: UUID,
        run_id: UUID,
        actor_id: str,
        approved_url: str,
        expires_at: datetime,
        idempotency_key: UUID,
        viewport_width: int,
        viewport_height: int,
        max_page_bytes: int,
        timeout_seconds: int,
        limits: BrowserTargetLimits,
        now: datetime,
    ) -> "BrowserTarget":
        target = cls(
            project_id=project_id,
            run_id=run_id,
            actor_id=actor_id,
            approved_url=approved_url,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            max_page_bytes=max_page_bytes,
            timeout_seconds=timeout_seconds,
            limits=limits,
            issued_at=now,
        )
        return target

    @property
    def origin(self) -> str:
        return _parse_exact_local_url(self.approved_url, self.limits.max_url_chars)[0]

    @property
    def path(self) -> str:
        return _parse_exact_local_url(self.approved_url, self.limits.max_url_chars)[1]

    @property
    def binding_digest(self) -> str:
        payload = {
            "project_id": str(self.project_id),
            "run_id": str(self.run_id),
            "actor_id": self.actor_id,
            "approved_url": self.approved_url,
            "expires_at": _aware_utc(self.expires_at, "expires_at").isoformat(),
            "issued_at": _aware_utc(self.issued_at, "issued_at").isoformat(),
            "idempotency_key": str(self.idempotency_key),
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "max_page_bytes": self.max_page_bytes,
            "timeout_seconds": self.timeout_seconds,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def check_navigation(self, observed_url: str, *, now: datetime, redirect: bool = False) -> None:
        if redirect:
            raise BrowserTargetError("browser redirect is forbidden")
        checked_at = _aware_utc(now, "now")
        if checked_at < _aware_utc(self.issued_at, "issued_at"):
            raise BrowserTargetError("browser target was not yet issued")
        if checked_at >= _aware_utc(self.expires_at, "expires_at"):
            raise BrowserTargetError("browser target expired")
        _parse_exact_local_url(observed_url, self.limits.max_url_chars)
        if observed_url != self.approved_url:
            raise BrowserTargetError("browser target drift")
