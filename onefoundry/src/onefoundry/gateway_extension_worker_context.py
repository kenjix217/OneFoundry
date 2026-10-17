"""Bind host protocol files back to gateway-owned invocation and capability records."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_extension_registry import ExtensionTool, definitions, digest, pinned
from onefoundry.gateway_extension_spool import job_digest, read_json
from onefoundry.role_tool_admission import ToolDenied, _context

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def admitted_parent(
    gateway: ToolGateway,
    root: Path,
    invocation_id: UUID,
    statuses: frozenset[str],
    *,
    job_path: Path | None = None,
) -> tuple[dict[str, Any], ExtensionTool, dict[str, Any]]:
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("extension settings are unavailable")
    job = read_json(job_path or root / "jobs" / f"{invocation_id}.json", 7 * 1024 * 1024)
    if job.get("schema_version") != 1 or job.get("job_sha256") != job_digest(job):
        raise ToolDenied("extension worker job identity changed")
    with gateway.engine.begin() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM extension_invocations WHERE id=:id"), {"id": invocation_id}
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] not in statuses:
            raise ToolDenied("extension parent invocation is not available")
        saved = dict(row)
        context = _context(
            connection,
            saved["project_id"],
            saved["run_id"],
            saved["profile_id"],
            lock=False,
            max_items=settings.role_context_max_items,
            allow_completed=True,
        )
        tool = next(
            (item for item in definitions(connection, saved["project_id"]) if item.id == saved["tool_id"]),
            None,
        )
        if (
            tool is None
            or tool.id not in context.current_tools & context.snapshot_tools
            or not pinned(
                connection,
                saved["project_id"],
                saved["run_id"],
                saved["profile_id"],
                tool,
                settings.extension_worker_image,
            )
        ):
            raise ToolDenied("extension parent capability was revoked or changed")
    expected = {
        "invocation_id": str(invocation_id),
        "project_id": str(saved["project_id"]),
        "run_id": str(saved["run_id"]),
        "profile_id": str(saved["profile_id"]),
        "actor_id": f"profile:{saved['profile_id']}",
        "package_sha256": tool.package_sha256,
        "tool_id": tool.spec.id,
    }
    if any(job.get(key) != value for key, value in expected.items()) or digest(
        job.get("arguments")
    ) != digest(saved["arguments"]):
        raise ToolDenied("extension worker job differs from its admitted invocation")
    return saved, tool, job
