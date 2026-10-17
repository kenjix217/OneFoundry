"""Project connection lifecycle and mailbox-bound read access owned by the gateway."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.smtp_schemas import address
from onefoundry.zoho_mail_contracts import ZohoCredentials, ZohoRead, ZohoSetup
from onefoundry.zoho_mail_mcp import HostedZohoClient, ZohoClient, ZohoMcpError, provider_data

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

COLUMNS = (
    "id,label,status,enabled,mail_account_id,from_address,last_error_code,last_probe_at,version,"
    "credential_revision,created_at"
)
ClientFactory = Callable[[str, str, float, int], ZohoClient]


class ZohoMailStore:
    def __init__(
        self,
        gateway: ToolGateway,
        settings: Settings,
        client_factory: ClientFactory = HostedZohoClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.gateway, self.settings, self.client_factory = gateway, settings, client_factory
        self.clock = clock
        self.secrets = ModelSecretStore(str(Path(settings.model_secret_root) / "zoho-mail"))

    def prepare_secrets(self) -> None:
        ModelSecretStore(self.settings.model_secret_root).check_ready()
        self.secrets.root.mkdir(mode=0o700, exist_ok=True)
        self.secrets.check_ready()

    def row(
        self, connection: Any, project_id: UUID, connection_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    f"SELECT project_id,{COLUMNS} FROM zoho_mail_connections WHERE project_id=:p AND id=:id"  # noqa: S608
                    + (" FOR UPDATE" if lock else "")
                ),
                {"p": project_id, "id": connection_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("Zoho Mail connection not found")
        return dict(row)

    def protected(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(base64.urlsafe_b64decode(self.secrets.read(row["id"])))
            if not isinstance(value, dict) or value.get("version") != row["version"]:
                raise ValueError
            revision = row.get("credential_revision")
            if revision is None:
                with self.gateway.engine.connect() as connection:
                    revision = connection.scalar(
                        text(
                            "SELECT credential_revision FROM zoho_mail_connections "
                            "WHERE id=:id AND version=:v"
                        ),
                        {"id": row["id"], "v": row["version"]},
                    )
            if value.get("credential_revision", 1) != revision:
                raise ValueError
            if not isinstance(value.get("server_url"), str) or not value["server_url"]:
                raise ValueError
            return value
        except (SecretStoreError, ValueError):
            raise Unavailable("Zoho Mail credentials are missing or changed; save them again") from None

    def vault(self, row: dict[str, Any]) -> dict[str, Any]:
        value = self.protected(row)
        if not isinstance(value.get("access_token"), str) or not value["access_token"]:
            raise Unavailable("Connect to Zoho to authorize this mailbox")
        return value

    def write_protected(self, connection: Any, row: dict[str, Any], value: dict[str, Any]) -> None:
        value = {**value, "version": row["version"], "credential_revision": row["credential_revision"] + 1}
        try:
            self.secrets.write(row["id"], base64.urlsafe_b64encode(json.dumps(value).encode()).decode())
        except SecretStoreError:
            raise Unavailable("Zoho credential store is unavailable") from None
        connection.execute(
            text(
                "UPDATE zoho_mail_connections SET credential_revision=credential_revision+1 "
                "WHERE id=:id AND project_id=:p"
            ),
            {"id": row["id"], "p": row["project_id"]},
        )

    def replace_authorization(
        self, connection: Any, row: dict[str, Any], value: dict[str, Any], event: str
    ) -> dict[str, Any]:
        row = {**row, "version": row["version"] + 1}
        connection.execute(
            text(
                "UPDATE zoho_mail_connections SET version=version+1,status=:status,enabled=false,"
                "mail_account_id=NULL,from_address=NULL,last_error_code=NULL,last_probe_at=NULL "
                "WHERE project_id=:p AND id=:id"
            ),
            {
                "p": row["project_id"],
                "id": row["id"],
                "status": "unverified" if value.get("access_token") else "needs_credentials",
            },
        )
        self.write_protected(connection, row, value)
        self.gateway._event(
            connection, row["project_id"], None, None, event, {"connection_id": str(row["id"])}
        )
        return self.row(connection, row["project_id"], row["id"])

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            protected = self.protected(row)
            endpoint_present = True
        except Unavailable:
            protected, endpoint_present = {}, False
        try:
            self.vault(row)
            present = True
        except Unavailable:
            present = False
        return {
            **{key: value for key, value in row.items() if key not in {"project_id", "credential_revision"}},
            "credential_present": present,
            "endpoint_present": endpoint_present,
            "oauth_pending": protected.get("pending", {}).get("phase") == "waiting"
            and protected.get("pending", {}).get("expires_at", 0) > self.clock(),
            "status": row["status"] if present else "needs_credentials",
            "enabled": bool(row["enabled"] and present),
        }

    def list(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            rows = connection.execute(
                text(
                    f"SELECT {COLUMNS} FROM zoho_mail_connections WHERE project_id=:p ORDER BY created_at,id"  # noqa: S608
                ),
                {"p": project_id},
            ).mappings()
            return [self.public(dict(row)) for row in rows]

    def create(self, project_id: UUID, data: ZohoSetup) -> dict[str, Any]:
        connection_id = uuid4()
        with self.gateway.engine.begin() as connection:
            self.gateway._require_project(connection, project_id)
            inserted = connection.execute(
                text(
                    "INSERT INTO zoho_mail_connections (id,project_id,actor_id,label) VALUES "
                    "(:id,:p,:actor,:label) ON CONFLICT (project_id,label) DO NOTHING"
                ),
                {"id": connection_id, "p": project_id, "actor": self.gateway.actor_id, "label": data.label},
            )
            if inserted.rowcount != 1:
                raise Conflict("a Zoho connection with this label already exists")
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "zoho_mail_setup_saved",
                {"connection_id": str(connection_id)},
            )
            return self.public(self.row(connection, project_id, connection_id))

    def credentials(self, project_id: UUID, connection_id: UUID, data: ZohoCredentials) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            row = self.row(connection, project_id, connection_id, lock=True)
            if row["version"] != data.expected_version:
                raise Conflict("Zoho connection changed; refresh before saving credentials")
            value = {
                "server_url": data.server_url.get_secret_value(),
                "access_token": data.access_token.get_secret_value(),
                "mail_account_id": data.mail_account_id,
            }
            changed = self.replace_authorization(connection, row, value, "zoho_mail_credentials_saved")
            return self.public(changed)

    def client(self, row: dict[str, Any]) -> ZohoClient:
        from onefoundry.zoho_mail_signin import ZohoSignInStore

        vault = ZohoSignInStore(self).access(row)
        return self.client_factory(
            vault["server_url"],
            vault["access_token"],
            float(self.settings.connector_http_timeout_seconds),
            self.settings.mail_max_message_bytes,
        )

    def probe(self, project_id: UUID, connection_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = self.row(connection, project_id, connection_id)
        vault = self.vault(row)
        selected_id, sender, error = None, None, None
        try:
            accounts = provider_data(self.client(row).call("getMailAccounts", {}))
            if not isinstance(accounts, list) or not accounts:
                raise ZohoMcpError("mailbox_unavailable")
            selected = [
                item
                for item in accounts
                if isinstance(item, dict)
                and (not vault["mail_account_id"] or str(item.get("accountId")) == vault["mail_account_id"])
            ]
            if len(selected) != 1:
                raise ZohoMcpError("select_mailbox_account")
            selected_id = str(selected[0].get("accountId", ""))
            if not selected_id.isdigit() or len(selected_id) > 30:
                raise ZohoMcpError("invalid_response")
            sender = address(str(selected[0].get("primaryEmailAddress", "")))
        except (ZohoMcpError, ValueError) as failure:
            error = failure.code if isinstance(failure, ZohoMcpError) else "invalid_response"
            selected_id, sender = None, None
        with self.gateway.engine.begin() as connection:
            current = self.row(connection, project_id, connection_id, lock=True)
            if current["version"] != row["version"]:
                raise Conflict("Zoho connection changed during the check; check again")
            connection.execute(
                text(
                    "UPDATE zoho_mail_connections SET status=:status,enabled=false,mail_account_id=:account,"
                    "from_address=:sender,last_error_code=:error,last_probe_at=clock_timestamp() "
                    "WHERE project_id=:p AND id=:id"
                ),
                {
                    "status": "ready" if error is None else "failed",
                    "account": selected_id,
                    "sender": sender,
                    "error": error,
                    "p": project_id,
                    "id": connection_id,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "zoho_mail_checked",
                {"connection_id": str(connection_id), "error_code": error},
            )
            return self.public(self.row(connection, project_id, connection_id))

    def set_enabled(
        self, project_id: UUID, connection_id: UUID, enabled: bool, expected_version: int
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            row = self.row(connection, project_id, connection_id, lock=True)
            if row["version"] != expected_version:
                raise Conflict("Zoho connection changed; refresh before updating")
            if enabled:
                self.vault(row)
                if row["status"] != "ready":
                    raise Conflict("check Zoho credentials before enabling access")
            # Configuration revisions invalidate proposals; update the protected revision too.
            if self.public(row)["credential_present"]:
                vault = self.vault(row)
                vault["version"] = row["version"] + 1
                self.secrets.write(
                    connection_id, base64.urlsafe_b64encode(json.dumps(vault).encode()).decode()
                )
            connection.execute(
                text(
                    "UPDATE zoho_mail_connections SET enabled=:enabled,version=version+1 "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"enabled": enabled, "p": project_id, "id": connection_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "zoho_mail_enabled_changed",
                {"connection_id": str(connection_id), "enabled": enabled},
            )
            return self.public(self.row(connection, project_id, connection_id))

    def ready(self, connection: Any, project_id: UUID, connection_id: UUID) -> dict[str, Any]:
        row = self.row(connection, project_id, connection_id, lock=True)
        if row["status"] != "ready" or not row["enabled"]:
            raise Conflict("Zoho Mail connection must be checked and enabled first")
        self.vault(row)
        return row

    def authentication_failed(self, project_id: UUID, row: dict[str, Any]) -> None:
        with self.gateway.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE zoho_mail_connections SET status='failed',enabled=false,"
                    "last_error_code='authentication_failed' WHERE project_id=:p AND id=:id AND version=:v"
                ),
                {"p": project_id, "id": row["id"], "v": row["version"]},
            )
            if changed.rowcount == 1:
                self.gateway._event(
                    connection,
                    project_id,
                    None,
                    None,
                    "zoho_mail_access_failed",
                    {"connection_id": str(row["id"]), "error_code": "authentication_failed"},
                )

    def read(self, project_id: UUID, data: ZohoRead, *, actor_id: str | None = None) -> dict[str, Any]:
        if data.operation == "connections":
            rows = [row for row in self.list(project_id) if row["enabled"] and row["status"] == "ready"]
            return {
                "connections": [
                    {
                        "id": str(row["id"]),
                        "label": row["label"],
                        "from_address": row["from_address"],
                        "mail_account_id": row["mail_account_id"],
                    }
                    for row in rows[: self.settings.role_context_max_items]
                ]
            }
        connection_id = data.connection_id
        if connection_id is None:
            raise Conflict("choose a Zoho connection before reading mail")
        with self.gateway.engine.begin() as connection:
            row = self.ready(connection, project_id, connection_id)
        args: dict[str, Any] = {"path_variables": {"accountId": row["mail_account_id"]}}
        if data.operation == "folders":
            tool = "getAllFolders"
            args["query_params"] = {"fields": "folderId,folderName,folderType"}
        elif data.operation == "list":
            tool = "listEmails"
            args["query_params"] = {
                "fields": "subject,messageId,folderId,fromAddress,toAddress,receivedTime",
                "limit": data.limit,
            }
            if data.folder_id:
                args["query_params"]["folderId"] = data.folder_id
        elif data.operation == "search":
            tool = "SearchEmails"
            args["query_params"] = {"searchKey": data.search_key, "limit": data.limit}
        else:
            tool = {
                "content": "getMessageContent",
                "details": "getMessageDetails",
                "headers": "getMessageHeader",
            }[data.operation]
            args["path_variables"].update(folderId=data.folder_id, messageId=data.message_id)
        try:
            result = self.client(row).call(tool, args)
        except ZohoMcpError as error:
            if error.code == "authentication_failed":
                self.authentication_failed(project_id, row)
            raise
        provider_data(result)
        with self.gateway.engine.begin() as connection:
            current = self.ready(connection, project_id, connection_id)
            if current["version"] != row["version"]:
                raise Conflict("Zoho access changed while reading")
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "zoho_mail_read",
                {"connection_id": str(data.connection_id), "operation": data.operation},
                actor_id=actor_id,
            )
        return result
