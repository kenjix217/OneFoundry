"""Watched drive folders: new or changed files become input files and document signals.

A source is one folder in one connected account. Only that folder's direct
children are read. Each new file revision is fetched once, saved through the
input-file pipeline (so the restricted worker extracts its text), and recorded as
``document.created`` or ``document.changed``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry import coordination_store
from onefoundry.document_api_sources import (
    Change,
    ChangeBatch,
    DocumentError,
    GoogleDriveFolder,
    OneDriveFolder,
    folder_client,
)
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.inference.http import HTTPTransport, send_http
from onefoundry.input_file_bytes import InputFileInvalid
from onefoundry.input_file_store import InputFileStore

if TYPE_CHECKING:
    from onefoundry.connector_store import ConnectorStore
    from onefoundry.document_schemas import DocumentSourceCreate
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

COLUMNS = (
    "id,connector_account_id,provider,label,folder_ref,folder_id,folder_name,enabled,status,last_error_code,"
    "last_probe_at,last_poll_at,version,created_at"
)
PROVIDER_ACCOUNT = {"onedrive": "microsoft", "gdrive": "google"}


class DocumentStore:
    def __init__(
        self,
        gateway: ToolGateway,
        settings: Settings,
        connectors: ConnectorStore,
        http: HTTPTransport = send_http,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.connectors = connectors
        self.http = http
        gateway.document_store = self

    def client(self, project_id: UUID, row: dict[str, Any]) -> OneDriveFolder | GoogleDriveFolder:
        connectors, connector_id = self.connectors, row["connector_account_id"]
        return folder_client(
            row["provider"],
            lambda: connectors.access_token(project_id, connector_id),
            http=self.http,
            timeout=float(self.settings.connector_http_timeout_seconds),
        )

    def _row(
        self, connection: Any, project_id: UUID, source_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    f"SELECT {COLUMNS},cursor,project_id FROM document_sources "  # noqa: S608
                    "WHERE project_id=:p AND id=:id" + (" FOR UPDATE" if lock else "")
                ),
                {"p": project_id, "id": source_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("document source not found")
        return dict(row)

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in COLUMNS.split(",")}

    def list(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT {COLUMNS} FROM document_sources WHERE project_id=:p ORDER BY created_at,id"),  # noqa: S608
                {"p": project_id},
            ).mappings()
            return [self._public(dict(row)) for row in rows]

    def create(self, project_id: UUID, data: DocumentSourceCreate) -> dict[str, Any]:
        source_id = uuid4()
        with self.gateway.engine.begin() as connection:
            connector = (
                connection.execute(
                    text("SELECT provider,features FROM connector_accounts WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": data.connector_account_id},
                )
                .mappings()
                .one_or_none()
            )
            if connector is None:
                raise NotFound("connected account not found")
            if connector["provider"] != PROVIDER_ACCOUNT[data.provider] or "files" not in list(
                connector["features"]
            ):
                raise Conflict("choose an account of the same provider connected with file access")
            inserted = connection.execute(
                text(
                    "INSERT INTO document_sources "
                    "(id,project_id,actor_id,connector_account_id,provider,label,folder_ref) "
                    "VALUES (:id,:p,:actor,:connector,:provider,:label,:ref) ON CONFLICT "
                    "(project_id,label) DO NOTHING"
                ),
                {
                    "id": source_id,
                    "p": project_id,
                    "actor": self.gateway.actor_id,
                    "connector": data.connector_account_id,
                    "provider": data.provider,
                    "label": data.label,
                    "ref": data.folder_ref,
                },
            )
            if inserted.rowcount != 1:
                raise Conflict("a document source with this label already exists")
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "document_source_saved",
                {"source_id": str(source_id), "provider": data.provider},
            )
            return self._public(self._row(connection, project_id, source_id))

    def probe(self, project_id: UUID, source_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, source_id)
        error: str | None = None
        folder_id = folder_name = cursor = None
        try:
            client = self.client(project_id, row)
            folder = client.resolve(row["folder_ref"])
            folder_id, folder_name = folder.folder_id, folder.name
            cursor = row["cursor"] if row["folder_id"] == folder_id and row["cursor"] else client.baseline()
        except DocumentError as failure:
            error = failure.code
        except Unavailable:
            error = "credential_unavailable"
        with self.gateway.engine.begin() as connection:
            self._row(connection, project_id, source_id, lock=True)
            connection.execute(
                text(
                    "UPDATE document_sources SET status=:s,enabled=CASE WHEN :s='ready' THEN enabled ELSE "
                    "false END,"
                    "last_error_code=:e,folder_id=COALESCE(:f,folder_id),folder_name=COALESCE(:n,folder_name),"
                    "cursor=COALESCE(:c,cursor),last_probe_at=clock_timestamp(),version=version+1 "
                    "WHERE project_id=:p AND id=:id"
                ),
                {
                    "s": "failed" if error else "ready",
                    "e": error,
                    "f": folder_id,
                    "n": folder_name,
                    "c": cursor,
                    "p": project_id,
                    "id": source_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "document_source_probed",
                {"source_id": str(source_id), "status": "failed" if error else "ready", "error_code": error},
            )
            return self._public(self._row(connection, project_id, source_id))

    def set_enabled(self, project_id: UUID, source_id: UUID, enabled: bool, expected: int) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            row = self._row(connection, project_id, source_id, lock=True)
            if row["version"] != expected:
                raise Conflict("document source changed; reload before saving")
            if enabled and row["status"] != "ready":
                raise Conflict("check the folder successfully before turning it on")
            connection.execute(
                text(
                    "UPDATE document_sources SET enabled=:e,version=version+1 WHERE project_id=:p AND id=:id"
                ),
                {"e": enabled, "p": project_id, "id": source_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "document_source_enabled" if enabled else "document_source_disabled",
                {"source_id": str(source_id)},
            )
            return self._public(self._row(connection, project_id, source_id))

    def delete(self, project_id: UUID, source_id: UUID, expected: int) -> None:
        with self.gateway.engine.begin() as connection:
            row = self._row(connection, project_id, source_id, lock=True)
            if row["version"] != expected:
                raise Conflict("document source changed; reload before removing")
            connection.execute(
                text("DELETE FROM document_files WHERE project_id=:p AND source_id=:id"),
                {"p": project_id, "id": source_id},
            )
            connection.execute(
                text("DELETE FROM document_sources WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": source_id},
            )
            self.gateway._event(
                connection, project_id, None, None, "document_source_removed", {"source_id": str(source_id)}
            )

    def _claim(self) -> dict[str, Any] | None:
        with self.gateway.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT d.project_id,d.id FROM document_sources d JOIN projects p ON "
                        "p.id=d.project_id "
                        "WHERE d.enabled AND d.status='ready' AND p.status='active' "
                        "AND (d.poll_lease_until IS NULL OR d.poll_lease_until<clock_timestamp()) "
                        "AND (d.last_poll_at IS NULL OR "
                        "d.last_poll_at<clock_timestamp()-(:every * interval '1 second')) "
                        "AND NOT EXISTS(SELECT 1 FROM coordination_controls c "
                        "WHERE c.project_id=d.project_id AND c.paused) "
                        "ORDER BY d.last_poll_at NULLS FIRST,d.id FOR UPDATE OF d SKIP LOCKED LIMIT 1"
                    ),
                    {"every": self.settings.document_poll_seconds},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            connection.execute(
                text(
                    "UPDATE document_sources SET poll_lease_until=clock_timestamp()+(:s * interval '1 "
                    "second') "
                    "WHERE project_id=:p AND id=:id"
                ),
                {
                    "s": self.settings.connector_http_timeout_seconds * 20 + 30,
                    "p": row["project_id"],
                    "id": row["id"],
                },
            )
            return self._row(connection, row["project_id"], row["id"])

    def _ingest(
        self, row: dict[str, Any], client: OneDriveFolder | GoogleDriveFolder, change: Change
    ) -> dict[str, Any]:
        """Fetch and save one file revision; failures are recorded on the signal, not raised."""
        pid = row["project_id"]
        try:
            name, data = client.download(change, self.settings.document_max_bytes)
            saved = InputFileStore(self.gateway, self.settings).ingest(pid, name, data)
            return {
                "input_file_id": str(saved["id"]),
                "input_file_sha256": saved["sha256"],
                "ingest_error": None,
            }
        except DocumentError as failure:
            return {"input_file_id": None, "ingest_error": failure.code}
        except InputFileInvalid:
            return {"input_file_id": None, "ingest_error": "unsupported_or_too_large"}
        except Conflict:
            return {"input_file_id": None, "ingest_error": "input_file_quota"}

    def poll_next(self) -> bool:
        if self.settings.coordination_mode == "off":
            return False
        row = self._claim()
        if row is None:
            return False
        pid, source_id = row["project_id"], row["id"]
        try:
            client = self.client(pid, row)
            try:
                batch = client.changes(
                    str(row["cursor"]), str(row["folder_id"]), self.settings.document_max_files_per_poll
                )
            except DocumentError as failure:
                if failure.code != "cursor_expired":
                    raise
                batch = ChangeBatch((), client.baseline())
        except (DocumentError, Unavailable) as failure:
            code = failure.code if isinstance(failure, DocumentError) else "credential_unavailable"
            with self.gateway.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE document_sources SET last_error_code=:e,last_poll_at=clock_timestamp(),"
                        "poll_lease_until=NULL,"
                        "status=CASE WHEN :e IN ('authentication_failed','credential_unavailable') "
                        "THEN 'failed' ELSE status END,"
                        "enabled=CASE WHEN :e IN ('authentication_failed','credential_unavailable') "
                        "THEN false ELSE enabled END WHERE project_id=:p AND id=:id"
                    ),
                    {"e": code, "p": pid, "id": source_id},
                )
                self.gateway._event(
                    connection,
                    pid,
                    None,
                    None,
                    "document_poll_failed",
                    {"source_id": str(source_id), "error_code": code},
                )
            return True
        recorded = 0
        for change in batch.changes:
            with self.gateway.engine.connect() as connection:
                known = connection.execute(
                    text(
                        "SELECT revision FROM document_files WHERE project_id=:p AND source_id=:s "
                        "AND provider_file_id=:f"
                    ),
                    {"p": pid, "s": source_id, "f": change.file_id},
                ).scalar_one_or_none()
            if known == change.revision:
                continue
            outcome = self._ingest(row, client, change)
            with self.gateway.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO document_files "
                        "(id,project_id,actor_id,source_id,provider_file_id,name,revision,"
                        "input_file_id) VALUES (:id,:p,:actor,:s,:f,:name,:rev,:input) "
                        "ON CONFLICT (project_id,source_id,provider_file_id) DO UPDATE SET "
                        "name=EXCLUDED.name,"
                        "revision=EXCLUDED.revision,input_file_id=EXCLUDED.input_file_id,updated_at=clock_timestamp()"
                    ),
                    {
                        "id": uuid4(),
                        "p": pid,
                        "actor": self.gateway.actor_id,
                        "s": source_id,
                        "f": change.file_id,
                        "name": change.name,
                        "rev": change.revision,
                        "input": outcome["input_file_id"],
                    },
                )
                signal_id = coordination_store.record_signal(
                    self.gateway,
                    connection,
                    pid,
                    source="document",
                    signal_type="document.changed" if known is not None else "document.created",
                    dedupe_key=f"document:{source_id}:{change.file_id}:{change.revision}"[:300],
                    fields={
                        "source_id": str(source_id),
                        "source_label": row["label"],
                        "provider": row["provider"],
                        "file_id": change.file_id,
                        "file_name": change.name,
                        "mime_type": change.mime_type,
                        "size": change.size,
                        "modified_at": change.modified_at,
                        "revision": change.revision[:200],
                        **outcome,
                    },
                )
                recorded += signal_id is not None
        with self.gateway.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE document_sources SET "
                    "cursor=:c,last_poll_at=clock_timestamp(),poll_lease_until=NULL,"
                    "last_error_code=NULL WHERE project_id=:p AND id=:id"
                ),
                {"c": batch.cursor[:4000], "p": pid, "id": source_id},
            )
            self.gateway._event(
                connection,
                pid,
                None,
                None,
                "document_polled",
                {"source_id": str(source_id), "new_revisions": recorded},
            )
        return True
