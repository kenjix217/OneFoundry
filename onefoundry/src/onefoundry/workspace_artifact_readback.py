"""Project-scoped owner readback for private role-authored text artifacts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.artifact_workspace import ArtifactWorkspaceError
from onefoundry.gateway_errors import Conflict, NotFound

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def list_artifacts(gateway: ToolGateway, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        if (
            connection.scalar(
                text("SELECT 1 FROM runs WHERE project_id=:project_id AND id=:run_id"),
                {"project_id": project_id, "run_id": run_id},
            )
            is None
        ):
            raise NotFound("run not found")
        rows = (
            connection.execute(
                text(
                    "SELECT id,kind,mime_type,sha256,byte_size,status,created_at,finished_at "
                    "FROM workspace_artifacts WHERE project_id=:project_id AND run_id=:run_id "
                    "ORDER BY created_at DESC,id DESC LIMIT 100"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def preview(gateway: ToolGateway, project_id: UUID, run_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    with gateway.engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id,kind,mime_type,relative_path,sha256,byte_size,status "
                    "FROM workspace_artifacts WHERE project_id=:project_id AND run_id=:run_id AND id=:id"
                ),
                {"project_id": project_id, "run_id": run_id, "id": artifact_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise NotFound("artifact not found")
    if row["status"] != "ready":
        raise Conflict("artifact outcome is not ready")
    try:
        data = gateway._artifact_workspace().read(
            project_id,
            run_id,
            artifact_id,
            relative_path=row["relative_path"],
            expected_sha256=row["sha256"],
            expected_byte_size=row["byte_size"],
            kind=row["kind"],
            mime_type=row["mime_type"],
            max_video_bytes=gateway.live_text_settings.media_max_bytes if gateway.live_text_settings else 0,
        )
        if row["kind"] == "video":
            edit_receipt = None
            if gateway.live_text_settings and gateway.live_text_settings.video_edit_enabled:
                with gateway.engine.connect() as connection:
                    edit_receipt = connection.scalar(
                        text(
                            "SELECT receipt FROM video_edit_jobs WHERE project_id=:p AND run_id=:r "
                            "AND artifact_id=:id AND status='done'"
                        ),
                        {"p": project_id, "r": run_id, "id": artifact_id},
                    )
            with gateway.engine.connect() as connection:
                caption = connection.scalar(
                    text(
                        "SELECT payload->>'caption' FROM events WHERE project_id=:p AND run_id=:r "
                        "AND event_type='media_capture_requested' AND payload->>'capture_id'= "
                        "(SELECT payload->>'capture_id' FROM events WHERE project_id=:p AND run_id=:r "
                        "AND event_type='media_save_started' AND payload->>'artifact_id'=:id LIMIT 1) LIMIT 1"
                    ),
                    {"p": project_id, "r": run_id, "id": str(artifact_id)},
                )
                verified = (
                    connection.scalar(
                        text(
                            "SELECT 1 FROM events WHERE project_id=:p AND run_id=:r AND "
                            "event_type='media_playback_verified' "
                            "AND payload->>'artifact_id'=:id AND payload->>'sha256'=:sha LIMIT 1"
                        ),
                        {"p": project_id, "r": run_id, "id": str(artifact_id), "sha": row["sha256"]},
                    )
                    is not None
                )
            content = f"Recorded video bytes are held locally. Caption: {caption or 'not recorded'}. " + (
                "The operator watched and verified this exact video; QA must still review its claims."
                if verified
                else "Operator playback verification is still required before QA can pass this video."
            )
            if edit_receipt:
                content = "Rendered local video; exact render/QC provenance: " + json.dumps(
                    edit_receipt, sort_keys=True
                )
                content += (
                    " Operator playback and independent QA are still required."
                    if not verified
                    else " Operator playback is verified."
                )
        else:
            content = data.decode("utf-8", errors="strict")
    except (ArtifactWorkspaceError, OSError, UnicodeError) as error:
        raise Conflict("artifact readback failed") from error
    result = {
        "id": row["id"],
        "kind": row["kind"],
        "mime_type": row["mime_type"],
        "sha256": row["sha256"],
        "byte_size": row["byte_size"],
        "content": content,
    }
    if row["kind"] == "video":
        result["playback_verified"] = verified
    return result


def binary(gateway: ToolGateway, project_id: UUID, run_id: UUID, artifact_id: UUID) -> bytes:
    """Read exact owner-bound WebM bytes; metadata is not a storage path supplied by a caller."""
    with gateway.engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT relative_path,sha256,byte_size,status,kind,mime_type FROM workspace_artifacts "
                    "WHERE project_id=:p AND run_id=:r AND id=:id"
                ),
                {"p": project_id, "r": run_id, "id": artifact_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise NotFound("video artifact not found")
    if row["kind"] != "video" or row["status"] != "ready":
        raise Conflict("video bytes are not ready")
    try:
        return gateway._artifact_workspace().read(
            project_id,
            run_id,
            artifact_id,
            relative_path=row["relative_path"],
            expected_sha256=row["sha256"],
            expected_byte_size=row["byte_size"],
            kind="video",
            mime_type="video/webm",
            max_video_bytes=gateway.live_text_settings.media_max_bytes if gateway.live_text_settings else 0,
        )
    except (ArtifactWorkspaceError, OSError) as error:
        raise Conflict("video artifact readback failed") from error
