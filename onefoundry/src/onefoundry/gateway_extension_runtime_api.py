"""Operator controls for gateway-owned credentials, tests and exact saved calls."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.gateway_extension_calls import public_receipt
from onefoundry.gateway_extension_control import cancel_invocation
from onefoundry.gateway_extension_credentials import credential_headers, write_credential
from onefoundry.gateway_extension_runtime import execute_saved
from onefoundry.gateway_extension_schemas import ConnectionConfig
from onefoundry.gateway_extension_transport import ExtensionTransport, TransportFailure
from onefoundry.gateway_extension_worker_readiness import worker_status
from onefoundry.role_tool_admission import ToolInputError, ToolOutcomeUnknown, _reject_sensitive_output
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel


class CredentialPut(StrictModel):
    expected_version: int = Field(ge=1)
    value: str = Field(min_length=1, max_length=8192, repr=False)


class ProbeCreate(StrictModel):
    expected_version: int = Field(ge=1)
    confirm_endpoint: str = Field(max_length=2048)


def _readiness(gateway: ToolGateway, project_id: UUID, connection_id: UUID) -> dict[str, Any]:
    saved = next(
        (row for row in gateway._extensions().list_connections(project_id) if row["id"] == connection_id),
        None,
    )
    if saved is None:
        raise NotFound("extension connection not found")
    config = ConnectionConfig.model_validate(saved["config"])
    settings = gateway.live_text_settings
    present = config.auth_method == "none"
    if settings is not None:
        with gateway.engine.connect() as connection:
            try:
                credential_headers(connection, settings, project_id, connection_id, config)
                present = True
            except (Conflict, Unavailable):
                present = False
            tested = connection.scalar(
                text(
                    "SELECT payload->>'status' FROM events WHERE project_id=:p "
                    "AND event_type='extension_connection_tested' AND payload->>'connection_id'=:id "
                    "AND payload->>'version'=:version ORDER BY created_at DESC,id DESC LIMIT 1"
                ),
                {"p": project_id, "id": str(connection_id), "version": str(saved["version"])},
            )
            used = connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM extension_invocations WHERE project_id=:p "
                    "AND connection_id=:id AND connection_version=:version AND status='done')"
                ),
                {"p": project_id, "id": connection_id, "version": saved["version"]},
            )
    else:
        tested, used = None, False
    return {
        "version": saved["version"],
        "credential_present": present,
        "configured": bool(config.endpoint or config.stdio_module),
        "enabled": saved["enabled"],
        "live_test_status": tested or "not_tested",
        "successful_use": bool(used),
    }


def _probe(gateway: ToolGateway, project_id: UUID, connection_id: UUID, data: ProbeCreate) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("extension settings are unavailable")
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        saved = (
            connection.execute(
                text(
                    "SELECT config,version,package_id,template_id FROM extension_connections "
                    "WHERE project_id=:p AND id=:id FOR UPDATE"
                ),
                {"p": project_id, "id": connection_id},
            )
            .mappings()
            .one_or_none()
        )
        if saved is None:
            raise NotFound("extension connection not found")
        config = ConnectionConfig.model_validate(saved["config"])
        if saved["version"] != data.expected_version or config.endpoint != data.confirm_endpoint:
            raise Conflict("connection changed; review the destination before testing")
        if config.transport == "stdio":
            raise Conflict("stdio connections are verified by an isolated invocation")
        headers = credential_headers(connection, settings, project_id, connection_id, config)
        manifest = connection.scalar(
            text("SELECT manifest FROM extension_packages WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": saved["package_id"]},
        )
        template = next(item for item in manifest["connections"] if item["id"] == saved["template_id"])
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "extension_connection_test_started",
            {
                "connection_id": str(connection_id),
                "version": data.expected_version,
                "probe_kind": "mcp_discovery" if template["kind"] == "mcp" else "http_head",
            },
        )
    client = ExtensionTransport(
        tuple(settings.extension_allowed_origins),
        timeout=settings.extension_timeout_seconds,
        max_bytes=settings.extension_max_response_bytes,
    )
    result: dict[str, Any]
    try:
        if template["kind"] == "mcp":
            result = {"tools": client.discover(config.endpoint, headers)}
        else:
            from urllib.parse import urlsplit

            client.request(config.endpoint, "HEAD", urlsplit(config.endpoint).path or "/", None, headers)
            result = {"http_head": "accepted"}
        _reject_sensitive_output(result)
        if any(value.removeprefix("Bearer ") in str(result) for value in headers.values()):
            raise ValueError("protected_response")
    except Exception as error:
        reason = str(error) if isinstance(error, TransportFailure) else "probe_failed"
        with gateway.engine.begin() as connection:
            gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_connection_tested",
                {
                    "connection_id": str(connection_id),
                    "version": data.expected_version,
                    "status": "failed",
                    "reason": reason,
                },
            )
        raise Unavailable(f"connection test did not pass: {reason}") from None
    with gateway.engine.begin() as connection:
        current = connection.scalar(
            text("SELECT version FROM extension_connections WHERE project_id=:p AND id=:id FOR SHARE"),
            {"p": project_id, "id": connection_id},
        )
        if current != data.expected_version:
            raise Conflict("connection changed during its test; result is stale")
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "extension_connection_tested",
            {"connection_id": str(connection_id), "version": data.expected_version, "status": "passed"},
        )
    return {"status": "passed", "version": data.expected_version, **result}


def make_extension_runtime_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/extensions")

    @router.get("/worker")
    def worker(project_id: UUID) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            with gateway.engine.connect() as connection:
                gateway._require_project(connection, project_id)
            return (
                worker_status(gateway.live_text_settings)
                if gateway.live_text_settings
                else {"available": False, "reason": "not_configured"}
            )

        return execute(read)

    @router.put("/connections/{connection_id}/credential")
    def credential(project_id: UUID, connection_id: UUID, data: CredentialPut) -> dict[str, Any]:
        return execute(
            lambda: write_credential(gateway, project_id, connection_id, data.value, data.expected_version)
        )

    @router.get("/connections/{connection_id}/readiness")
    def readiness(project_id: UUID, connection_id: UUID) -> dict[str, Any]:
        return execute(lambda: _readiness(gateway, project_id, connection_id))

    @router.post("/connections/{connection_id}/probe")
    def probe(project_id: UUID, connection_id: UUID, data: ProbeCreate) -> dict[str, Any]:
        return execute(lambda: _probe(gateway, project_id, connection_id, data))

    @router.get("/invocations")
    def invocations(project_id: UUID) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            with gateway.engine.connect() as connection:
                gateway._require_project(connection, project_id)
                rows = connection.execute(
                    text(
                        "SELECT * FROM extension_invocations WHERE project_id=:p "
                        "ORDER BY created_at DESC,id DESC LIMIT 100"
                    ),
                    {"p": project_id},
                ).mappings()
                return [public_receipt(dict(row)) for row in rows]

        return execute(read)

    @router.post("/invocations/{invocation_id}/execute")
    def dispatch(project_id: UUID, invocation_id: UUID) -> dict[str, Any]:
        try:
            return execute(lambda: execute_saved(gateway, project_id, invocation_id))
        except ToolOutcomeUnknown:
            raise HTTPException(
                409, "extension outcome is unknown; inspect its saved receipt before any action"
            ) from None
        except ToolInputError:
            raise HTTPException(422, "extension input does not match its registered contract") from None

    @router.post("/invocations/{invocation_id}/cancel")
    def cancel(project_id: UUID, invocation_id: UUID) -> dict[str, Any]:
        return execute(lambda: cancel_invocation(gateway, project_id, invocation_id))

    return router
