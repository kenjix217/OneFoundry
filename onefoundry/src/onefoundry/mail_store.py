"""Mailbox connections owned by the Tool Gateway; new mail becomes ``email.received`` signals.

Passwords live only in the protected secret store. Message bytes are saved to the
private artifact volume with their SHA-256; signals carry bounded, normalized
fields and a short snippet that downstream work treats as quoted data.
"""

from __future__ import annotations

import base64
import hashlib
import os
import ssl
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import coordination_store
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.inference.http import HTTPTransport, send_http
from onefoundry.mail_api_sources import ApiBatch, GmailMailbox, GraphMailbox
from onefoundry.mail_api_sources import mailbox as api_mailbox
from onefoundry.mail_imap import Batch, ImapMailbox, MailConfig, MailError, parse_message
from onefoundry.mail_lineage import reply_lineage
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError

if TYPE_CHECKING:
    from onefoundry.connector_store import ConnectorStore
    from onefoundry.mail_schemas import MailAccountCreate, MailApiAccountCreate
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

COLUMNS = (
    "id,label,protocol,host,port,username,folder,enabled,status,last_error_code,capabilities,"
    "last_probe_at,last_poll_at,version,created_at,connector_account_id"
)
MailboxFactory = Callable[[MailConfig], ImapMailbox]


class MailStore:
    def __init__(
        self,
        gateway: ToolGateway,
        settings: Settings,
        mailbox_factory: MailboxFactory | None = None,
        context: ssl.SSLContext | None = None,
        connectors: ConnectorStore | None = None,
        api_http: HTTPTransport = send_http,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.connectors = connectors
        self.api_http = api_http
        self.secrets = ModelSecretStore(str(Path(settings.model_secret_root) / "mail"))
        self.mailbox_factory = mailbox_factory or (lambda config: ImapMailbox(config, context))

    def prepare_secrets(self) -> None:
        ModelSecretStore(self.settings.model_secret_root).check_ready()
        self.secrets.root.mkdir(mode=0o700, exist_ok=True)
        self.secrets.check_ready()

    def _row(
        self, connection: Any, project_id: UUID, account_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    f"SELECT {COLUMNS},uidvalidity,last_uid,cursor,project_id FROM mail_accounts "  # noqa: S608
                    "WHERE project_id=:p AND id=:id" + (" FOR UPDATE" if lock else "")
                ),
                {"p": project_id, "id": account_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("mail account not found")
        return dict(row)

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        public = {key: row[key] for key in COLUMNS.split(",")}
        public["capabilities"] = list(row["capabilities"])
        public["credential_present"] = (
            self.secrets.present(row["id"])
            if row["protocol"] == "imap"
            else row["connector_account_id"] is not None
        )
        return public

    def _api(self, row: dict[str, Any]) -> GmailMailbox | GraphMailbox:
        if self.connectors is None:
            raise Unavailable("connected accounts are unavailable")
        connectors = self.connectors
        project_id, connector_id = row["project_id"], row["connector_account_id"]
        return api_mailbox(
            row["protocol"],
            lambda: connectors.access_token(project_id, connector_id),
            http=self.api_http,
            timeout=float(self.settings.mail_timeout_seconds),
            max_bytes=self.settings.mail_max_message_bytes,
        )

    def create_api(self, project_id: UUID, data: MailApiAccountCreate) -> dict[str, Any]:
        provider = "google" if data.protocol == "gmail" else "microsoft"
        account_id = uuid4()
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
            if connector["provider"] != provider or "mail" not in list(connector["features"]):
                raise Conflict(f"choose a {provider.title()} account connected with mail access")
            inserted = connection.execute(
                text(
                    "INSERT INTO mail_accounts (id,project_id,actor_id,label,protocol,connector_account_id) "
                    "VALUES (:id,:p,:actor,:label,:protocol,:connector) "
                    "ON CONFLICT (project_id,label) DO NOTHING"
                ),
                {
                    "id": account_id,
                    "p": project_id,
                    "actor": self.gateway.actor_id,
                    "label": data.label,
                    "protocol": data.protocol,
                    "connector": data.connector_account_id,
                },
            )
            if inserted.rowcount != 1:
                raise Conflict("a mail account with this label already exists")
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "mail_account_saved",
                {"account_id": str(account_id), "protocol": data.protocol},
            )
            return self._public(self._row(connection, project_id, account_id))

    def list(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT {COLUMNS} FROM mail_accounts WHERE project_id=:p ORDER BY created_at,id"),  # noqa: S608
                {"p": project_id},
            ).mappings()
            return [self._public(dict(row)) for row in rows]

    def create(self, project_id: UUID, data: MailAccountCreate) -> dict[str, Any]:
        account_id = uuid4()
        try:
            # App passwords may contain spaces; the store accepts printable ASCII without them.
            encoded = base64.urlsafe_b64encode(data.password.get_secret_value().encode()).decode()
            self.secrets.write(account_id, encoded)
        except SecretStoreError:
            raise Unavailable("mail credential store is unavailable") from None
        try:
            with self.gateway.engine.begin() as connection:
                inserted = connection.execute(
                    text(
                        "INSERT INTO mail_accounts (id,project_id,actor_id,label,host,port,username,folder) "
                        "VALUES (:id,:p,:actor,:label,:host,:port,:user,:folder) "
                        "ON CONFLICT (project_id,label) DO NOTHING"
                    ),
                    {
                        "id": account_id,
                        "p": project_id,
                        "actor": self.gateway.actor_id,
                        "label": data.label,
                        "host": data.host,
                        "port": data.port,
                        "user": data.username,
                        "folder": data.folder,
                    },
                )
                if inserted.rowcount != 1:
                    raise Conflict("a mail account with this label already exists")
                self.gateway._event(
                    connection,
                    project_id,
                    None,
                    None,
                    "mail_account_saved",
                    {
                        "account_id": str(account_id),
                        "host": data.host,
                        "port": data.port,
                        "folder": data.folder,
                    },
                )
                return self._public(self._row(connection, project_id, account_id))
        except Exception:
            self.secrets.remove(account_id)
            raise

    def _config(self, row: dict[str, Any]) -> MailConfig:
        try:
            password = base64.urlsafe_b64decode(self.secrets.read(row["id"])).decode()
        except (SecretStoreError, ValueError):
            raise Unavailable("mail credential is unavailable; save the account again") from None
        return MailConfig(
            host=row["host"],
            port=int(row["port"]),
            username=row["username"],
            password=password,
            folder=row["folder"],
            timeout_seconds=float(self.settings.mail_timeout_seconds),
            max_message_bytes=self.settings.mail_max_message_bytes,
        )

    def read_only_config(
        self,
        project_id: UUID,
        account_id: UUID,
        *,
        host: str,
        port: int,
        username: str,
        folder: str,
    ) -> MailConfig:
        """Resolve a verified, pinned IMAP account without enabling mail polling."""
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, account_id)
        if (
            row["protocol"] != "imap"
            or row["status"] != "ready"
            or (row["host"], row["port"], row["username"]) != (host, port, username)
        ):
            raise Conflict("mail plugin account is unverified or its binding changed")
        return replace(self._config(row), folder=folder)

    def probe(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, account_id)
        if row["protocol"] != "imap":
            return self._probe_api(project_id, account_id, row)
        config = self._config(row)
        try:
            result = self.mailbox_factory(config).probe()
            error: str | None = None
        except MailError as failure:
            result, error = None, failure.code
        with self.gateway.engine.begin() as connection:
            current = self._row(connection, project_id, account_id, lock=True)
            if result is None:
                connection.execute(
                    text(
                        "UPDATE mail_accounts SET status='failed',enabled=false,last_error_code=:e,"
                        "last_probe_at=clock_timestamp(),version=version+1 WHERE project_id=:p AND id=:id"
                    ),
                    {"e": error, "p": project_id, "id": account_id},
                )
            else:
                same_mailbox = (
                    current["uidvalidity"] == result.uidvalidity and current["last_uid"] is not None
                )
                baseline = current["last_uid"] if same_mailbox else max((result.uidnext or 1) - 1, 0)
                connection.execute(
                    text(
                        "UPDATE mail_accounts SET status='ready',last_error_code=NULL,capabilities=:caps,"
                        "uidvalidity=:validity,last_uid=:baseline,last_probe_at=clock_timestamp(),"
                        "version=version+1 WHERE project_id=:p AND id=:id"
                    ).bindparams(bindparam("caps", type_=JSONB)),
                    {
                        "caps": list(result.capabilities)[:40],
                        "validity": result.uidvalidity,
                        "baseline": baseline,
                        "p": project_id,
                        "id": account_id,
                    },
                )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "mail_account_probed",
                {
                    "account_id": str(account_id),
                    "status": "failed" if result is None else "ready",
                    "error_code": error,
                    "idle": bool(result and result.idle),
                },
            )
            return self._public(self._row(connection, project_id, account_id))

    def _probe_api(self, project_id: UUID, account_id: UUID, row: dict[str, Any]) -> dict[str, Any]:
        row["project_id"] = project_id
        try:
            cursor: str | None = self._api(row).baseline()
            error: str | None = None
        except MailError as failure:
            cursor, error = None, failure.code
        except Unavailable:
            cursor, error = None, "credential_unavailable"
        with self.gateway.engine.begin() as connection:
            self._row(connection, project_id, account_id, lock=True)
            connection.execute(
                text(
                    "UPDATE mail_accounts SET status=:s,"
                    "enabled=CASE WHEN :s='ready' THEN enabled ELSE false END,"
                    "last_error_code=:e,cursor=COALESCE(:c,cursor),last_probe_at=clock_timestamp(),"
                    "version=version+1 WHERE project_id=:p AND id=:id"
                ),
                {
                    "s": "failed" if error else "ready",
                    "e": error,
                    "c": cursor if row["cursor"] is None else None,
                    "p": project_id,
                    "id": account_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "mail_account_probed",
                {
                    "account_id": str(account_id),
                    "status": "failed" if error else "ready",
                    "error_code": error,
                },
            )
            return self._public(self._row(connection, project_id, account_id))

    def set_enabled(self, project_id: UUID, account_id: UUID, enabled: bool, expected: int) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            row = self._row(connection, project_id, account_id, lock=True)
            if row["version"] != expected:
                raise Conflict("mail account changed; reload before saving")
            if enabled and row["status"] != "ready":
                raise Conflict("check the connection successfully before turning the mailbox on")
            connection.execute(
                text("UPDATE mail_accounts SET enabled=:e,version=version+1 WHERE project_id=:p AND id=:id"),
                {"e": enabled, "p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "mail_account_enabled" if enabled else "mail_account_disabled",
                {"account_id": str(account_id)},
            )
            return self._public(self._row(connection, project_id, account_id))

    def delete(self, project_id: UUID, account_id: UUID, expected: int) -> None:
        with self.gateway.engine.begin() as connection:
            row = self._row(connection, project_id, account_id, lock=True)
            if row["version"] != expected:
                raise Conflict("mail account changed; reload before removing")
            connection.execute(
                text("DELETE FROM mail_accounts WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection, project_id, None, None, "mail_account_removed", {"account_id": str(account_id)}
            )
        self.secrets.remove(account_id)

    def _claim(self) -> dict[str, Any] | None:
        with self.gateway.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        f"SELECT a.project_id,{','.join('a.' + c for c in COLUMNS.split(','))},"  # noqa: S608
                        "a.uidvalidity,a.last_uid,a.cursor FROM mail_accounts a "
                        "JOIN projects p ON p.id=a.project_id "
                        "WHERE a.enabled AND a.status='ready' AND p.status='active' "
                        "AND (a.poll_lease_until IS NULL OR a.poll_lease_until<clock_timestamp()) "
                        "AND (a.last_poll_at IS NULL OR "
                        "a.last_poll_at<clock_timestamp()-(:every * interval '1 second')) "
                        "AND NOT EXISTS(SELECT 1 FROM coordination_controls c "
                        "WHERE c.project_id=a.project_id AND c.paused) "
                        "ORDER BY a.last_poll_at NULLS FIRST,a.id "
                        "FOR UPDATE OF a SKIP LOCKED LIMIT 1"
                    ),
                    {"every": self.settings.mail_poll_seconds},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            connection.execute(
                text(
                    "UPDATE mail_accounts SET poll_lease_until=clock_timestamp()+(:s * interval '1 second') "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"s": self.settings.mail_timeout_seconds * 4 + 30, "p": row["project_id"], "id": row["id"]},
            )
            return dict(row)

    def _save_raw(self, row: dict[str, Any], uid: int | str, raw: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(raw).hexdigest()
        name = hashlib.sha256(str(uid).encode()).hexdigest()[:16] if isinstance(uid, str) else str(uid)
        relative = Path("mail") / str(row["project_id"]) / str(row["id"]) / f"{name}-{digest[:16]}.eml"
        target = Path(self.settings.artifact_workspace_root) / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return str(relative), digest
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
        return str(relative), digest

    def poll_next(self) -> bool:
        """Poll one due mailbox. Returns whether a mailbox was claimed."""
        if self.settings.coordination_mode == "off":
            return False
        row = self._claim()
        if row is None:
            return False
        pid, account_id = row["project_id"], row["id"]
        if row["protocol"] != "imap":
            return self._poll_api(row)
        try:
            mailbox = self.mailbox_factory(self._config(row))
            batch = mailbox.fetch_since(int(row["last_uid"] or 0), self.settings.mail_max_fetch_per_poll)
            baseline: int | None = None
            if row["last_uid"] is None or batch.uidvalidity != row["uidvalidity"]:
                # A new or rebuilt mailbox starts after its current newest message; nothing is backfilled.
                probe = mailbox.probe()
                batch = Batch(probe.uidvalidity, (), max((probe.uidnext or 1) - 1, 0))
                baseline = batch.highest_uid
        except (MailError, Unavailable) as failure:
            code = failure.code if isinstance(failure, MailError) else "credential_unavailable"
            with self.gateway.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE mail_accounts SET last_error_code=:e,last_poll_at=clock_timestamp(),"
                        "poll_lease_until=NULL,"
                        "status=CASE WHEN :e IN ('authentication_failed','credential_unavailable') "
                        "THEN 'failed' ELSE status END,"
                        "enabled=CASE WHEN :e IN ('authentication_failed','credential_unavailable') "
                        "THEN false ELSE enabled END WHERE project_id=:p AND id=:id"
                    ),
                    {"e": code, "p": pid, "id": account_id},
                )
                self.gateway._event(
                    connection,
                    pid,
                    None,
                    None,
                    "mail_poll_failed",
                    {"account_id": str(account_id), "error_code": code},
                )
            return True
        recorded = 0
        with self.gateway.engine.begin() as connection:
            if baseline is not None:
                self._finish_poll(connection, pid, account_id, batch.uidvalidity, batch.highest_uid)
                self.gateway._event(
                    connection,
                    pid,
                    None,
                    None,
                    "mail_baseline_reset",
                    {"account_id": str(account_id), "uidvalidity": batch.uidvalidity},
                )
                return True
            for message in batch.messages:
                parsed = parse_message(message.raw, self.settings.mail_max_snippet_chars)
                path, digest = self._save_raw(row, message.uid, message.raw)
                signal_id = coordination_store.record_signal(
                    self.gateway,
                    connection,
                    pid,
                    source="email",
                    signal_type="email.received",
                    dedupe_key=f"{row['host']}:{row['folder']}:{batch.uidvalidity}:{message.uid}"[:300],
                    **reply_lineage(connection, pid, parsed),
                    fields={
                        "account_id": str(account_id),
                        "account_label": row["label"],
                        "from_address": parsed.from_address,
                        "from_domain": parsed.from_domain,
                        "subject": parsed.subject,
                        "snippet": parsed.text,
                        "message_id": parsed.message_id,
                        "in_reply_to": parsed.in_reply_to,
                        "date": parsed.date,
                        "uid": message.uid,
                        "size": message.size,
                        "truncated": message.truncated,
                        "body_path": path,
                        "body_sha256": digest,
                    },
                )
                recorded += signal_id is not None
            self._finish_poll(connection, pid, account_id, batch.uidvalidity, batch.highest_uid)
            self.gateway._event(
                connection,
                pid,
                None,
                None,
                "mail_polled",
                {"account_id": str(account_id), "new_messages": recorded},
            )
        return True

    def _poll_api(self, row: dict[str, Any]) -> bool:
        pid, account_id = row["project_id"], row["id"]
        try:
            source = self._api(row)
            if row["cursor"] is None:
                batch = ApiBatch((), source.baseline())
            else:
                batch = source.fetch(str(row["cursor"]), self.settings.mail_max_fetch_per_poll)
        except MailError as failure:
            if failure.code == "cursor_expired":
                try:
                    batch = ApiBatch((), self._api(row).baseline())
                except (MailError, Unavailable):
                    return self._poll_failed(pid, account_id, "cursor_expired")
            else:
                return self._poll_failed(pid, account_id, failure.code)
        except Unavailable:
            return self._poll_failed(pid, account_id, "credential_unavailable")
        recorded = 0
        with self.gateway.engine.begin() as connection:
            for message in batch.messages:
                parsed = parse_message(message.raw, self.settings.mail_max_snippet_chars)
                path, digest = self._save_raw(row, message.message_id, message.raw)
                signal_id = coordination_store.record_signal(
                    self.gateway,
                    connection,
                    pid,
                    source="email",
                    signal_type="email.received",
                    dedupe_key=f"{row['protocol']}:{account_id}:{message.message_id}"[:300],
                    **reply_lineage(connection, pid, parsed),
                    fields=self._fields(
                        row, parsed, message.message_id, message.size, message.truncated, path, digest
                    ),
                )
                recorded += signal_id is not None
            connection.execute(
                text(
                    "UPDATE mail_accounts SET cursor=:c,last_poll_at=clock_timestamp(),poll_lease_until=NULL,"
                    "last_error_code=NULL WHERE project_id=:p AND id=:id"
                ),
                {"c": batch.cursor[:4000], "p": pid, "id": account_id},
            )
            self.gateway._event(
                connection,
                pid,
                None,
                None,
                "mail_polled",
                {"account_id": str(account_id), "new_messages": recorded, "skipped": batch.skipped},
            )
        return True

    def _poll_failed(self, project_id: UUID, account_id: UUID, code: str) -> bool:
        with self.gateway.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE mail_accounts SET last_error_code=:e,last_poll_at=clock_timestamp(),"
                    "poll_lease_until=NULL,"
                    "status=CASE WHEN :e IN ('authentication_failed','credential_unavailable') "
                    "THEN 'failed' ELSE status END,"
                    "enabled=CASE WHEN :e IN ('authentication_failed','credential_unavailable') "
                    "THEN false ELSE enabled END WHERE project_id=:p AND id=:id"
                ),
                {"e": code, "p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "mail_poll_failed",
                {"account_id": str(account_id), "error_code": code},
            )
        return True

    @staticmethod
    def _fields(
        row: dict[str, Any], parsed: Any, uid: object, size: int, truncated: bool, path: str, digest: str
    ) -> dict[str, Any]:
        return {
            "account_id": str(row["id"]),
            "account_label": row["label"],
            "from_address": parsed.from_address,
            "from_domain": parsed.from_domain,
            "subject": parsed.subject,
            "snippet": parsed.text,
            "message_id": parsed.message_id,
            "in_reply_to": parsed.in_reply_to,
            "date": parsed.date,
            "uid": uid,
            "size": size,
            "truncated": truncated,
            "body_path": path,
            "body_sha256": digest,
        }

    @staticmethod
    def _finish_poll(
        connection: Any, project_id: UUID, account_id: UUID, validity: int, last_uid: int
    ) -> None:
        connection.execute(
            text(
                "UPDATE mail_accounts SET uidvalidity=:v,last_uid=:u,last_poll_at=clock_timestamp(),"
                "poll_lease_until=NULL,last_error_code=NULL WHERE project_id=:p AND id=:id"
            ),
            {"v": validity, "u": last_uid, "p": project_id, "id": account_id},
        )
