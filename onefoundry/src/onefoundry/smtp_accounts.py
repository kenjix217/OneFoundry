"""Project-owned SMTP identities with protected, write-only credentials."""

from __future__ import annotations

import base64
import smtplib
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.smtp_schemas import SmtpAccountCreate
from onefoundry.smtp_transport import SmtpConfig, probe

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

COLUMNS = (
    "id,label,host,port,security,username,from_address,status,enabled,"
    "last_error_code,last_probe_at,version,created_at"
)


class SmtpAccounts:
    def __init__(
        self, gateway: ToolGateway, settings: Settings, check: Callable[[SmtpConfig], None] = probe
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.secrets = ModelSecretStore(str(Path(settings.model_secret_root) / "smtp"))
        self.check = check

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
                    f"SELECT {COLUMNS} FROM smtp_accounts WHERE project_id=:p AND id=:id"  # noqa: S608
                    + (" FOR UPDATE" if lock else "")  # noqa: S608
                ),
                {"p": project_id, "id": account_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("SMTP account not found")
        return dict(row)

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "credential_present": self.secrets.present(row["id"])}

    def list(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            rows = connection.execute(
                text(f"SELECT {COLUMNS} FROM smtp_accounts WHERE project_id=:p ORDER BY created_at,id"),  # noqa: S608
                {"p": project_id},
            ).mappings()
            return [self._public(dict(row)) for row in rows]

    def create(self, project_id: UUID, data: SmtpAccountCreate) -> dict[str, Any]:
        account_id = uuid4()
        try:
            self.secrets.write(
                account_id, base64.urlsafe_b64encode(data.password.get_secret_value().encode()).decode()
            )
        except SecretStoreError:
            raise Unavailable("SMTP credential store is unavailable") from None
        try:
            with self.gateway.engine.begin() as connection:
                self.gateway._require_project(connection, project_id)
                inserted = connection.execute(
                    text(
                        "INSERT INTO smtp_accounts "
                        "(id,project_id,actor_id,label,host,port,security,username,from_address) "
                        "VALUES (:id,:p,:actor,:label,:host,:port,:security,:username,:sender) "
                        "ON CONFLICT (project_id,label) DO NOTHING"
                    ),
                    {
                        "id": account_id,
                        "p": project_id,
                        "actor": self.gateway.actor_id,
                        "label": data.label,
                        "host": data.host,
                        "port": data.port,
                        "security": data.security,
                        "username": data.username,
                        "sender": data.from_address,
                    },
                )
                if inserted.rowcount != 1:
                    raise Conflict("an SMTP account with this label already exists")
                self.gateway._event(
                    connection, project_id, None, None, "smtp_account_saved", {"account_id": str(account_id)}
                )
                return self._public(self._row(connection, project_id, account_id))
        except Exception:
            self.secrets.remove(account_id)
            raise

    def config(self, row: dict[str, Any]) -> SmtpConfig:
        try:
            password = base64.urlsafe_b64decode(self.secrets.read(row["id"])).decode()
        except (SecretStoreError, ValueError, UnicodeDecodeError):
            raise Unavailable("SMTP credential is unavailable") from None
        return SmtpConfig(
            row["host"],
            row["port"],
            row["security"],
            row["username"],
            password,
            float(self.settings.smtp_timeout_seconds),
        )

    def probe(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, account_id)
        config = self.config(row)
        try:
            self.check(config)
            error: str | None = None
        except smtplib.SMTPAuthenticationError:
            error = "authentication_failed"
        except ssl.SSLError:
            error = "tls_failed"
        except (OSError, smtplib.SMTPException, ValueError):
            error = "connection_failed"
        with self.gateway.engine.begin() as connection:
            current = self._row(connection, project_id, account_id, lock=True)
            if current["version"] != row["version"]:
                raise Conflict("SMTP account changed during connection check; check again")
            connection.execute(
                text(
                    "UPDATE smtp_accounts SET status=:status,"
                    "enabled=:enabled,last_error_code=:error,"
                    "last_probe_at=clock_timestamp(),version=version+1 WHERE project_id=:p AND id=:id"
                ),
                {
                    "status": "ready" if error is None else "failed",
                    "enabled": bool(current["enabled"] and error is None),
                    "error": error,
                    "p": project_id,
                    "id": account_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "smtp_account_checked",
                {
                    "account_id": str(account_id),
                    "status": "ready" if error is None else "failed",
                    "error_code": error,
                },
            )
            return self._public(self._row(connection, project_id, account_id))

    def set_enabled(
        self, project_id: UUID, account_id: UUID, enabled: bool, expected_version: int
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            row = self._row(connection, project_id, account_id, lock=True)
            if row["version"] != expected_version:
                raise Conflict("SMTP account changed; refresh before updating")
            if enabled and (row["status"] != "ready" or not self.secrets.present(account_id)):
                raise Conflict("check SMTP credentials before enabling sending")
            connection.execute(
                text(
                    "UPDATE smtp_accounts SET enabled=:enabled,version=version+1 "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"enabled": enabled, "p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "smtp_account_enabled_changed",
                {"account_id": str(account_id), "enabled": enabled},
            )
            return self._public(self._row(connection, project_id, account_id))
