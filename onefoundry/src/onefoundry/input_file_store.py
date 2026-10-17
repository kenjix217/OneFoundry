"""Project-owned input upload, links, and metadata readback."""

# _COLUMNS is a module constant; all request values use SQL binds.
# ruff: noqa: S608

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.input_file_bytes import InputFileBytes, InputFileInvalid, StoredInputFile
from onefoundry.input_file_extract import ALL_EXTENSIONS
from onefoundry.profile_hierarchy import lock_project

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

_COLUMNS = (
    "f.id,f.project_id,f.display_name,f.extension,f.mime_type,f.sha256,f.byte_size,"
    "f.status,f.reason_code,f.extractor_version,f.created_at,f.finished_at"
)


class InputFileStore:
    def __init__(self, gateway: ToolGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.settings = settings

    def _bytes(self) -> InputFileBytes:
        return InputFileBytes(self.settings.input_file_root)

    @staticmethod
    def _name(value: str) -> tuple[str, str]:
        if (
            not value
            or len(value) > 255
            or value != value.strip()
            or any(ord(char) < 32 or char in "/\\" for char in value)
        ):
            raise InputFileInvalid("file name is invalid")
        extension = value.rsplit(".", 1)[-1].casefold() if "." in value else ""
        if extension not in ALL_EXTENSIONS:
            raise InputFileInvalid("unsupported file format")
        return value, extension

    def _get(self, connection: Any, project_id: UUID, file_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    f"SELECT {_COLUMNS} FROM input_files f WHERE f.project_id=:project_id AND f.id=:file_id"
                ),
                {"project_id": project_id, "file_id": file_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("input file not found")
        return dict(row)

    def get(self, project_id: UUID, file_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            return self._get(connection, project_id, file_id)

    def list(self, project_id: UUID, work_order_id: UUID | None = None) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            if connection.scalar(text("SELECT 1 FROM projects WHERE id=:id"), {"id": project_id}) is None:
                raise NotFound("project not found")
            where = (
                "l.scope='project'"
                if work_order_id is None
                else "(l.scope='project' OR l.work_order_id=:order_id)"
            )
            rows = (
                connection.execute(
                    text(
                        f"SELECT DISTINCT {_COLUMNS} FROM input_files f "
                        "JOIN input_file_links l ON l.project_id=f.project_id AND l.file_id=f.id "
                        f"WHERE f.project_id=:project_id AND {where} "
                        "ORDER BY f.created_at,f.id"
                    ),
                    {"project_id": project_id, **({"order_id": work_order_id} if work_order_id else {})},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    async def upload(
        self,
        project_id: UUID,
        name: str,
        chunks: AsyncIterable[bytes],
        *,
        scope: str,
        work_order_id: UUID | None,
        file_id: UUID | None = None,
    ) -> dict[str, Any]:
        name, extension = self._name(name)
        if scope not in {"project", "work_order", "staged"} or (scope == "project") != (
            work_order_id is None
        ):
            raise InputFileInvalid("input file scope is invalid")
        file_id = file_id or uuid4()
        original = self._bytes().write_async(
            project_id, file_id, chunks, max_bytes=self.settings.input_file_max_bytes
        )
        saved = await original
        try:
            return self._record(project_id, name, extension, saved, scope, work_order_id, file_id)
        except BaseException:
            self._bytes().remove(project_id, file_id)
            raise

    def _record(
        self,
        project_id: UUID,
        name: str,
        extension: str,
        saved: StoredInputFile,
        scope: str,
        work_order_id: UUID | None,
        file_id: UUID,
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            if (
                work_order_id is not None
                and connection.scalar(
                    text("SELECT 1 FROM work_orders WHERE project_id=:project_id AND id=:order_id"),
                    {"project_id": project_id, "order_id": work_order_id},
                )
                is None
            ):
                raise NotFound("work order not found")
            raw_count, raw_size = connection.execute(
                text(
                    "SELECT count(*),coalesce(sum(byte_size),0) FROM input_files "
                    "WHERE project_id=:project_id AND status!='rejected'"
                ),
                {"project_id": project_id},
            ).one()
            count, size = cast(int, raw_count), cast(int, raw_size)
            if (
                count >= self.settings.input_file_max_project_count
                or size + saved.byte_size > self.settings.input_file_max_project_bytes
            ):
                raise Conflict("project input-file quota reached")
            if scope == "work_order":
                linked_count = connection.scalar(
                    text(
                        "SELECT count(*) FROM input_file_links "
                        "WHERE project_id=:project_id AND work_order_id=:order_id "
                        "AND scope='work_order'"
                    ),
                    {"project_id": project_id, "order_id": work_order_id},
                )
                if linked_count >= self.settings.input_file_max_link_count:
                    raise Conflict("work order input-file limit reached")
            mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            connection.execute(
                text(
                    "INSERT INTO input_files (id,project_id,actor_id,display_name,extension,mime_type,"
                    "relative_path,sha256,byte_size,status) VALUES "
                    "(:id,:project_id,:actor_id,:name,:extension,:mime_type,:path,:sha,:size,'pending')"
                ),
                {
                    "id": file_id,
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "name": name,
                    "extension": extension,
                    "mime_type": mime_type,
                    "path": saved.relative_path,
                    "sha": saved.sha256,
                    "size": saved.byte_size,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO input_file_links (id,project_id,actor_id,file_id,scope,work_order_id) "
                    "VALUES (:id,:project_id,:actor_id,:file_id,:scope,:work_order_id)"
                ),
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "file_id": file_id,
                    "scope": scope,
                    "work_order_id": work_order_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                work_order_id,
                None,
                "input_file_uploaded",
                {"file_id": str(file_id), "sha256": saved.sha256, "byte_size": saved.byte_size},
            )
            return self._get(connection, project_id, file_id)

    def ingest(self, project_id: UUID, name: str, data: bytes) -> dict[str, Any]:
        """Save fetched bytes as a pending project input file for the restricted worker."""
        name, extension = self._name(name)
        file_id = uuid4()
        saved = self._bytes().write(project_id, file_id, [data], max_bytes=self.settings.input_file_max_bytes)
        try:
            return self._record(project_id, name, extension, saved, "project", None, file_id)
        except BaseException:
            self._bytes().remove(project_id, file_id)
            raise
