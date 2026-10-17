"""Read-only, bounded view of a host-produced Compose service snapshot."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ServiceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    state: Literal["running", "stopped", "missing", "unknown"]
    health: Literal["healthy", "unhealthy", "starting", "none", "unknown"]

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9_-]*", value) is None:
            raise ValueError("invalid service name")
        return value


class ComposeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at: datetime
    services: list[ServiceSnapshot] = Field(min_length=1, max_length=16)

    @field_validator("captured_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("services")
    @classmethod
    def unique_services(cls, value: list[ServiceSnapshot]) -> list[ServiceSnapshot]:
        if len({service.name for service in value}) != len(value):
            raise ValueError("duplicate services")
        return value


def read_status(path: Path, max_age_seconds: int, now: datetime | None = None) -> dict[str, Any]:
    """Return unknown for absent, invalid, or stale host observations."""
    observed_at = now or datetime.now(UTC)
    unavailable: dict[str, Any] = {
        "status": "unknown",
        "captured_at": None,
        "services": [],
        "reason": "No current local Compose observation is available.",
    }
    try:
        if path.stat().st_size > 16_384:
            return unavailable
        snapshot = ComposeSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError):
        return unavailable
    age = observed_at - snapshot.captured_at.astimezone(UTC)
    if age < timedelta(seconds=-30) or age > timedelta(seconds=max_age_seconds):
        return unavailable
    services = [service.model_dump() for service in snapshot.services]
    degraded = any(
        service.state != "running" or service.health == "unhealthy" for service in snapshot.services
    )
    uncertain = any(service.health in {"unknown", "starting"} for service in snapshot.services)
    status = "degraded" if degraded else "unknown" if uncertain else "healthy"
    return {
        "status": status,
        "captured_at": snapshot.captured_at.isoformat(),
        "services": services,
        "reason": None,
    }


def make_infra_router(snapshot_path: Path, max_age_seconds: int) -> APIRouter:
    if max_age_seconds < 1 or max_age_seconds > 3600:
        raise ValueError("max_age_seconds must be between 1 and 3600")
    router = APIRouter(prefix="/api/infra")

    @router.get("/status")
    def status() -> dict[str, Any]:
        return read_status(snapshot_path, max_age_seconds)

    return router
