"""Gateway credential references over the existing protected credential store."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.gateway_extension_schemas import ConnectionConfig
from onefoundry.gateway_extension_transport import canonical_origin
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "cookie", "proxy-authorization"}
)


def credential_store(settings: Settings) -> ModelSecretStore:
    """Use the established private root while isolating unrelated cleanup owners."""
    ModelSecretStore(settings.model_secret_root).check_ready()
    root = Path(settings.model_secret_root) / "extension-connections"
    root.mkdir(mode=0o700, exist_ok=True)
    store = ModelSecretStore(str(root))
    store.check_ready()
    return store


def write_credential(
    gateway: ToolGateway,
    project_id: UUID,
    connection_id: UUID,
    value: str,
    expected_version: int,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("extension settings are unavailable")
    credential_id = uuid4()
    try:
        with gateway.engine.begin() as connection:
            gateway._require_project(connection, project_id)
            saved = (
                connection.execute(
                    text(
                        "SELECT config,version FROM extension_connections "
                        "WHERE project_id=:p AND id=:id FOR UPDATE"
                    ),
                    {"p": project_id, "id": connection_id},
                )
                .mappings()
                .one_or_none()
            )
            if saved is None:
                raise NotFound("extension connection not found")
            if saved["version"] != expected_version:
                raise Conflict("connection changed; reload before saving credentials")
            config = ConnectionConfig.model_validate(saved["config"])
            if config.auth_method == "none":
                raise Conflict("connection does not use a credential")
            if config.header_name and config.header_name.lower() in FORBIDDEN_HEADERS:
                raise Conflict("credential header is not permitted")
            credential_store(settings).write(credential_id, value)
            connection.execute(
                text(
                    "INSERT INTO extension_connection_credentials "
                    "(project_id,connection_id,actor_id,credential_id,origin,auth_method,header_name) "
                    "VALUES (:p,:id,:actor,:credential,:origin,:auth,:header) "
                    "ON CONFLICT (project_id,connection_id) DO UPDATE SET actor_id=EXCLUDED.actor_id,"
                    "credential_id=EXCLUDED.credential_id,origin=EXCLUDED.origin,"
                    "auth_method=EXCLUDED.auth_method,header_name=EXCLUDED.header_name"
                ),
                {
                    "p": project_id,
                    "id": connection_id,
                    "actor": gateway.actor_id,
                    "credential": credential_id,
                    "origin": canonical_origin(config.endpoint),
                    "auth": config.auth_method,
                    "header": config.header_name,
                },
            )
            connection.execute(
                text("UPDATE extension_connections SET version=version+1 WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": connection_id},
            )
            gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_credential_saved",
                {"connection_id": str(connection_id), "version": expected_version + 1},
            )
        return {"credential_present": True, "version": expected_version + 1}
    except SecretStoreError:
        raise Unavailable("protected credential storage is unavailable") from None


def credential_headers(
    connection: Connection,
    settings: Settings,
    project_id: UUID,
    connection_id: UUID,
    config: ConnectionConfig,
) -> dict[str, str]:
    if config.auth_method == "none":
        return {}
    record = (
        connection.execute(
            text(
                "SELECT credential_id,origin,auth_method,header_name FROM extension_connection_credentials "
                "WHERE project_id=:p AND connection_id=:id"
            ),
            {"p": project_id, "id": connection_id},
        )
        .mappings()
        .one_or_none()
    )
    if record is None or (record["origin"], record["auth_method"], record["header_name"]) != (
        canonical_origin(config.endpoint),
        config.auth_method,
        config.header_name,
    ):
        raise Conflict("connection credential must be configured for this destination")
    try:
        value = credential_store(settings).read(record["credential_id"])
    except SecretStoreError:
        raise Unavailable("protected connection credential is unavailable") from None
    if config.auth_method == "bearer":
        return {"Authorization": f"Bearer {value}"}
    if not config.header_name or config.header_name.lower() in FORBIDDEN_HEADERS:
        raise Conflict("credential header is not permitted")
    return {config.header_name: value}
