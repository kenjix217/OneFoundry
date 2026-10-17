"""Operator-owned, scoped media intake without document extraction or remote fetching."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.input_file_bytes import InputFileBytes, InputFileInvalid
from onefoundry.profile_hierarchy import lock_project
from onefoundry.role_sensitive_text import contains_sensitive_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

MIME_KIND = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "video/webm": "video",
    "video/mp4": "video",
    "audio/wav": "audio",
}


def validate_signature(mime: str, data: bytes) -> None:
    matched = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "video/webm": data.startswith(b"\x1aE\xdf\xa3") and b"webm" in data[:256],
        "video/mp4": data[4:8] == b"ftyp",
        "audio/wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
    }
    if not matched.get(mime):
        raise InputFileInvalid("media format does not match its declared type")


class VideoEditAssets:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        settings = gateway.live_text_settings
        if settings is None or not settings.video_edit_enabled:
            raise Conflict("local video editing is disabled")
        self.settings = settings
        self.bytes = InputFileBytes(settings.video_edit_asset_root)

    async def upload(
        self,
        project: UUID,
        mime: str,
        provenance: str,
        chunks: AsyncIterable[bytes],
        order: UUID | None = None,
    ) -> dict[str, Any]:
        if self.gateway.actor_id != self.settings.operator_actor_id:
            raise Conflict("media intake requires the operator boundary")
        if mime not in MIME_KIND or not 1 <= len(provenance) <= 240 or contains_sensitive_text(provenance):
            raise InputFileInvalid("media type or provenance is invalid")
        if any(ord(c) < 32 for c in provenance):
            raise InputFileInvalid("provenance must be plain text")
        asset_id = uuid4()
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project)
            if (
                order is not None
                and connection.scalar(
                    text("SELECT 1 FROM work_orders WHERE project_id=:p AND id=:o"),
                    {"p": project, "o": order},
                )
                is None
            ):
                raise NotFound("work order not found")
        saved = await self.bytes.write_async(
            project, asset_id, chunks, max_bytes=self.settings.media_max_bytes
        )
        try:
            data = self.bytes.read(project, asset_id, saved.sha256, saved.byte_size)
            validate_signature(mime, data)
            with self.gateway.engine.begin() as connection:
                lock_project(connection, project)
                count, size = connection.execute(
                    text(
                        "SELECT count(*),coalesce(sum(byte_size),0) FROM video_edit_assets WHERE "
                        "project_id=:p"
                    ),
                    {"p": project},
                ).one()
                if cast(int, count) >= self.settings.input_file_max_project_count or (
                    cast(int, size) + saved.byte_size > self.settings.input_file_max_project_bytes
                ):
                    raise Conflict("project media intake quota reached")
                connection.execute(
                    text(
                        "INSERT INTO video_edit_assets "
                        "(id,project_id,actor_id,work_order_id,kind,mime_type,sha256,byte_size,provenance) "
                        "VALUES (:id,:p,:actor,:o,:kind,:mime,:sha,:size,:provenance)"
                    ),
                    {
                        "id": asset_id,
                        "p": project,
                        "actor": self.gateway.actor_id,
                        "o": order,
                        "kind": MIME_KIND[mime],
                        "mime": mime,
                        "sha": saved.sha256,
                        "size": saved.byte_size,
                        "provenance": provenance,
                    },
                )
                self.gateway._event(
                    connection,
                    project,
                    order,
                    None,
                    "video_edit_asset_saved",
                    {
                        "asset_id": str(asset_id),
                        "kind": MIME_KIND[mime],
                        "sha256": saved.sha256,
                        "byte_size": saved.byte_size,
                    },
                )
            return {
                "asset_id": str(asset_id),
                "kind": MIME_KIND[mime],
                "sha256": saved.sha256,
                "byte_size": saved.byte_size,
            }
        except BaseException:
            self.bytes.remove(project, asset_id)
            raise
