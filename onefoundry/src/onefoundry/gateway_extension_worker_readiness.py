"""Read-only, expiring evidence that the separate supervisor accepts this policy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from onefoundry.gateway_extension_spool import read_json
from onefoundry.role_tool_admission import ToolDenied

if TYPE_CHECKING:
    from onefoundry.settings import Settings


def worker_status(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    if not settings.extension_worker_image:
        return {"available": False, "reason": "not_configured"}
    try:
        path = Path(settings.runner_spool_root) / "extensions" / "heartbeat.json"
        if any(parent.is_symlink() for parent in (path.parent, *path.parents)):
            raise ToolDenied("worker heartbeat path is unsafe")
        heartbeat = read_json(path, 4096)
        updated = datetime.fromisoformat(heartbeat["updated_at"])
        UUID(heartbeat["supervisor_id"])
        if heartbeat.get("schema_version") != 1 or updated.tzinfo is None:
            raise ValueError
        age = ((now or datetime.now(UTC)) - updated).total_seconds()
        if not 0 <= age <= settings.extension_worker_heartbeat_seconds:
            return {"available": False, "reason": "stale"}
        exact = {
            "image_id": settings.extension_worker_image,
            "memory_mb": settings.extension_worker_memory_mb,
            "processes": settings.extension_worker_processes,
            "cpus": settings.extension_worker_cpus,
        }
        if any(heartbeat.get(key) != value for key, value in exact.items()) or (
            type(heartbeat.get("max_timeout_seconds")) is not int
            or heartbeat["max_timeout_seconds"] < settings.extension_timeout_seconds
            or type(heartbeat.get("max_output_bytes")) is not int
            or heartbeat["max_output_bytes"] < settings.extension_worker_max_output_bytes
        ):
            return {"available": False, "reason": "policy_mismatch"}
        return {"available": True, "reason": "ready", "age_seconds": age}
    except (OSError, ValueError, KeyError, TypeError, ToolDenied):
        return {"available": False, "reason": "unavailable"}
