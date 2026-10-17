"""A configured image alone is not a live isolated supervisor connection."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from onefoundry.gateway_extension_spool import prepare_spool, publish_json
from onefoundry.gateway_extension_worker_readiness import worker_status
from onefoundry.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_password="test-only", runner_spool_root=str(tmp_path), extension_worker_image="sha256:" + "a" * 64
    )


def _heartbeat(settings: Settings, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "image_id": settings.extension_worker_image,
        "updated_at": now.isoformat(),
        "supervisor_id": str(uuid4()),
        "memory_mb": settings.extension_worker_memory_mb,
        "processes": settings.extension_worker_processes,
        "cpus": settings.extension_worker_cpus,
        "max_timeout_seconds": settings.extension_timeout_seconds,
        "max_output_bytes": settings.extension_worker_max_output_bytes,
    }


def test_image_configuration_does_not_prove_worker_availability(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert worker_status(settings) == {"available": False, "reason": "unavailable"}
    settings.extension_worker_image = ""
    assert worker_status(settings) == {"available": False, "reason": "not_configured"}


@pytest.mark.parametrize(
    "kind", ["valid", "stale", "future", "image", "resources", "timeout", "invalid", "public"]
)
def test_worker_heartbeat_is_private_fresh_and_exact(tmp_path: Path, kind: str) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    heartbeat = _heartbeat(settings, now)
    if kind == "stale":
        heartbeat["updated_at"] = (now - timedelta(seconds=31)).isoformat()
    elif kind == "future":
        heartbeat["updated_at"] = (now + timedelta(seconds=1)).isoformat()
    elif kind == "image":
        heartbeat["image_id"] = "sha256:" + "b" * 64
    elif kind == "resources":
        heartbeat["memory_mb"] += 128
    elif kind == "timeout":
        heartbeat["max_timeout_seconds"] = 0
    elif kind == "invalid":
        heartbeat["updated_at"] = "invalid"
    path = prepare_spool(settings) / "heartbeat.json"
    publish_json(path, heartbeat)
    if kind == "public":
        path.chmod(0o644)
    assert worker_status(settings, now=now)["available"] is (kind == "valid")
