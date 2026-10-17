"""Gateway extension admission and execution; remote I/O holds no DB locks."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from sqlalchemy import text

from onefoundry.gateway_extension_calls import claim, public_receipt, settle
from onefoundry.gateway_extension_credentials import credential_headers
from onefoundry.gateway_extension_registry import ExtensionTool, definitions, pinned
from onefoundry.gateway_extension_schemas import ConnectionConfig
from onefoundry.gateway_extension_spool import job_digest, prepare_spool, publish_json
from onefoundry.gateway_extension_transport import ExtensionTransport, TransportFailure
from onefoundry.gateway_extension_worker_readiness import worker_status
from onefoundry.role_tool_admission import (
    ToolDenied,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    _context,
    _reject_sensitive_output,
)

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway


def _validate(schema: dict[str, Any], value: object) -> None:
    def bounded_refs(part: object, depth: int = 0) -> None:
        if depth > 32:
            raise ToolInputError("tool schema is too deeply nested")
        if isinstance(part, dict):
            for key, child in part.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ToolInputError("remote schema references are not permitted")
                bounded_refs(child, depth + 1)
        elif isinstance(part, list):
            for child in part:
                bounded_refs(child, depth + 1)

    try:
        bounded_refs(schema)
        Draft202012Validator.check_schema(schema)
        if next(Draft202012Validator(schema).iter_errors(value), None) is not None:
            raise ToolInputError("value does not match the registered tool schema")
    except ToolInputError:
        raise
    except Exception:
        raise ToolInputError("invalid tool schema or value") from None


def _checked_result(tool: ExtensionTool, result: object, settings: Settings) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ToolOutputRejected("extension result must be an object")
    try:
        _validate(tool.spec.returns, result)
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > settings.extension_max_response_bytes:
            raise ValueError
    except (ValueError, ToolInputError):
        raise ToolOutputRejected("extension result failed its registered contract") from None
    _reject_sensitive_output(result)
    return result


def _worker_job(
    gateway: ToolGateway,
    tool: ExtensionTool,
    saved: dict[str, Any],
    settings: Settings,
) -> None:
    if not settings.extension_worker_image:
        raise ToolDenied("extension worker is not configured")
    if not worker_status(settings)["available"]:
        raise ToolDenied("extension supervisor is unavailable or its policy differs")
    with gateway.engine.connect() as connection:
        archive = connection.scalar(
            text(
                "SELECT archive FROM extension_packages WHERE project_id=:p "
                "AND id=:id AND sha256=:hash AND state='active'"
            ),
            {"p": saved["project_id"], "id": tool.package_id, "hash": tool.package_sha256},
        )
    if archive is None:
        raise ToolDenied("extension package is no longer active")
    job = {
        "schema_version": 1,
        "attempt_id": str(uuid4()),
        "expires_at": (
            datetime.now(UTC) + timedelta(seconds=settings.extension_worker_queue_seconds)
        ).isoformat(),
        "invocation_id": str(saved["id"]),
        "project_id": str(saved["project_id"]),
        "run_id": str(saved["run_id"]),
        "profile_id": str(saved["profile_id"]),
        "actor_id": f"profile:{saved['profile_id']}",
        "package_sha256": tool.package_sha256,
        "archive_base64": base64.b64encode(archive).decode(),
        "tool_id": tool.spec.id,
        "arguments": saved["arguments"],
        "timeout_seconds": settings.extension_timeout_seconds,
        "max_output_bytes": settings.extension_worker_max_output_bytes,
        "worker_image": settings.extension_worker_image,
        "memory_mb": settings.extension_worker_memory_mb,
        "processes": settings.extension_worker_processes,
        "cpus": settings.extension_worker_cpus,
        "execution_kind": "python",
    }
    if tool.spec.runtime == "mcp":
        config = ConnectionConfig.model_validate(tool.connection["config"] if tool.connection else {})
        job.update(
            execution_kind="mcp_stdio",
            stdio_module=config.stdio_module,
            operation=tool.spec.operation,
            expected_schema=tool.spec.returns,
        )
    job["job_sha256"] = job_digest(job)
    root = prepare_spool(settings)
    publish_json(root / "jobs" / f"{saved['id']}.json", job)


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    tool_id: str,
    arguments: object,
    tool_version: int,
    *,
    resume_saved: bool = False,
    callback_parent_id: UUID | None = None,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None or tool_version != 1:
        raise ToolDenied("extension tool configuration or version is unavailable")
    if not isinstance(arguments, dict):
        raise ToolInputError("extension arguments must be an object")
    try:
        encoded = json.dumps(arguments, allow_nan=False).encode()
    except (ValueError, TypeError):
        raise ToolInputError("extension arguments must be JSON") from None
    if len(encoded) > settings.extension_max_input_bytes:
        raise ToolInputError("extension input exceeds its configured bound")
    # The same sensitive-output guard also prevents credentials from entering
    # saved arguments, operator approval content, and a remote service request.
    _reject_sensitive_output(arguments)
    with gateway.engine.begin() as connection:
        context = _context(
            connection,
            project_id,
            run_id,
            profile_id,
            lock=True,
            max_items=settings.role_context_max_items,
            allow_completed=resume_saved,
        )
        tool = next((item for item in definitions(connection, project_id) if item.id == tool_id), None)
        if tool is None or tool_id not in context.snapshot_tools & context.current_tools:
            raise ToolDenied("extension tool is not admitted")
        if not pinned(connection, project_id, run_id, profile_id, tool, settings.extension_worker_image):
            raise ToolDenied("extension package or connection changed since the run snapshot")
        if (
            tool.spec.runtime == "python"
            or (tool.connection and tool.connection["config"].get("transport") == "stdio")
        ) and not worker_status(settings)["available"]:
            raise ToolDenied("extension supervisor is unavailable or its policy differs")
        _validate(tool.spec.parameters, arguments)
        if callback_parent_id is not None:
            parent = (
                connection.execute(
                    text(
                        "SELECT status,reason FROM extension_invocations WHERE project_id=:p AND id=:id "
                        "AND run_id=:run AND profile_id=:profile FOR UPDATE"
                    ),
                    {"p": project_id, "id": callback_parent_id, "run": run_id, "profile": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            root = prepare_spool(settings)
            if (
                parent is None
                or parent["status"] != "running"
                or parent["reason"] == "cancellation_requested"
                or (root / "cancellations" / f"{callback_parent_id}.json").exists()
            ):
                raise ToolDenied("extension callback parent is cancelled or unavailable")
        saved, execute = claim(gateway, connection, context, tool, call_id, arguments, execute_approved=True)
    if not execute:
        return public_receipt(saved)
    try:
        if tool.spec.runtime == "python" or (
            tool.connection and tool.connection["config"].get("transport") == "stdio"
        ):
            _worker_job(gateway, tool, saved, settings)
            return public_receipt(saved)
        assert tool.connection is not None
        config = ConnectionConfig.model_validate(tool.connection["config"])
        with gateway.engine.connect() as connection:
            headers = credential_headers(connection, settings, project_id, tool.connection["id"], config)
        client = ExtensionTransport(
            tuple(settings.extension_allowed_origins),
            timeout=settings.extension_timeout_seconds,
            max_bytes=settings.extension_max_response_bytes,
        )
        if tool.spec.runtime == "mcp":
            result = client.mcp(
                config.endpoint,
                tool.spec.operation or "",
                arguments,
                expected_schema=tool.spec.parameters,
                headers=headers,
            )
        else:
            template = next(item for item in tool.manifest.connections if item.id == tool.spec.connection)
            operation = next(item for item in template.operations if item.id == tool.spec.operation)
            if tool.spec.effect != "write" and operation.method != "GET":
                raise ToolDenied("non-GET HTTP operations must declare write effects")
            result = client.http(
                config.endpoint, operation.method or "", operation.path or "", arguments, headers
            )
        encoded_result = json.dumps(result, allow_nan=False)
        if any(secret.removeprefix("Bearer ") in encoded_result for secret in headers.values()):
            raise ToolOutputRejected("extension returned protected authentication material")
        result = _checked_result(tool, result, settings)
        # Revocation or configuration changes during remote I/O cannot release
        # its returned data into a provider context.
        with gateway.engine.begin() as connection:
            current = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
                allow_completed=resume_saved,
            )
            latest = next((item for item in definitions(connection, project_id) if item.id == tool_id), None)
            if (
                latest is None
                or tool_id not in current.snapshot_tools & current.current_tools
                or not pinned(
                    connection, project_id, run_id, profile_id, latest, settings.extension_worker_image
                )
            ):
                raise ToolDenied("extension permission changed during execution")
        settle(gateway, project_id, saved["id"], "done", result)
    except Exception as error:
        code = str(error) if isinstance(error, TransportFailure) else "execution_unconfirmed"
        terminal = "failed" if tool.spec.runtime == "python" else "unknown"
        settle(gateway, project_id, saved["id"], terminal, reason=code)
        if terminal == "unknown":
            raise ToolOutcomeUnknown(
                "extension effect or result is unconfirmed; inspect its receipt"
            ) from None
        raise ToolDenied("extension worker could not be queued") from None
    saved.update(status="done", result=result)
    return public_receipt(saved)


def execute_saved(gateway: ToolGateway, project_id: UUID, invocation_id: UUID) -> dict[str, Any]:
    with gateway.engine.connect() as connection:
        gateway._require_project(connection, project_id)
        saved = (
            connection.execute(
                text("SELECT * FROM extension_invocations WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": invocation_id},
            )
            .mappings()
            .one_or_none()
        )
        if saved is None:
            raise ToolDenied("extension invocation not found")
    return invoke(
        gateway,
        project_id,
        saved["run_id"],
        saved["profile_id"],
        saved["call_id"],
        saved["tool_id"],
        saved["arguments"],
        1,
        resume_saved=True,
    )
