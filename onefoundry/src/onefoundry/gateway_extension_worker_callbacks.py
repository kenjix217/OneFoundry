"""Admit isolated Python connection requests through the same Tool Gateway."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_extension_registry import definitions, pinned
from onefoundry.gateway_extension_runtime import invoke
from onefoundry.gateway_extension_spool import publish_json, read_json
from onefoundry.gateway_extension_worker_context import admitted_parent
from onefoundry.role_tool_admission import ToolDenied, ToolOutcomeUnknown, _context

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def process_callback(gateway: ToolGateway, root: Path, path: Path) -> None:
    response_path = root / "callbacks" / "responses" / path.name
    if response_path.exists():
        return
    request = read_json(path, 65536)
    invocation_id = UUID(request["invocation_id"])
    sequence = request.get("sequence")
    if (
        request.get("schema_version") != 1
        or type(sequence) is not int
        or not 1 <= sequence <= 16
        or path.name != f"{invocation_id}-{sequence}.json"
        or set(request)
        != {"schema_version", "invocation_id", "job_sha256", "sequence", "slot", "operation", "arguments"}
    ):
        raise ToolDenied("extension callback identity is invalid")
    response: dict[str, Any] = {
        "schema_version": 1,
        "invocation_id": str(invocation_id),
        "job_sha256": request["job_sha256"],
        "sequence": sequence,
    }
    try:
        saved, parent, job = admitted_parent(gateway, root, invocation_id, frozenset({"running"}))
        slot, operation = request["slot"], request["operation"]
        template = next((item for item in parent.manifest.connections if item.id == slot), None)
        if (
            job["job_sha256"] != request["job_sha256"]
            or parent.spec.runtime != "python"
            or parent.spec.effect != "pure"
            or slot not in parent.spec.required_connections
            or template is None
            or operation not in {item.id for item in template.operations}
        ):
            raise ToolDenied("extension callback is not declared")
        settings = gateway.live_text_settings
        assert settings is not None
        with gateway.engine.begin() as connection:
            context = _context(
                connection,
                saved["project_id"],
                saved["run_id"],
                saved["profile_id"],
                lock=False,
                max_items=settings.role_context_max_items,
                allow_completed=True,
            )
            targets = [
                item
                for item in definitions(connection, saved["project_id"])
                if item.package_id == parent.package_id
                and item.spec.connection == slot
                and item.spec.operation == operation
                and item.spec.runtime in {"http", "mcp"}
                and item.id in context.snapshot_tools & context.current_tools
                and pinned(
                    connection,
                    saved["project_id"],
                    saved["run_id"],
                    saved["profile_id"],
                    item,
                    settings.extension_worker_image,
                )
            ]
            if (
                len(targets) != 1
                or targets[0].connection is None
                or targets[0].connection["config"].get("transport") == "stdio"
            ):
                raise ToolDenied("extension callback needs one separately granted remote tool")
        receipt = invoke(
            gateway,
            saved["project_id"],
            saved["run_id"],
            saved["profile_id"],
            f"extension_cb_{invocation_id.hex}_{sequence}",
            targets[0].id,
            request["arguments"],
            1,
            resume_saved=True,
            callback_parent_id=invocation_id,
        )
        if receipt["status"] == "done":
            response.update(status="done", result=receipt["result"])
        elif receipt["status"] == "approval_required":
            response.update(
                status="approval_required",
                approval_id=receipt["approval_id"],
                waiting_invocation_id=receipt["invocation_id"],
            )
        else:
            response.update(status="failed", reason="connection_outcome_unconfirmed")
    except ToolOutcomeUnknown:
        with gateway.engine.connect() as connection:
            nested_id = connection.scalar(
                text(
                    "SELECT id FROM extension_invocations WHERE run_id=:run "
                    "AND project_id=:p AND call_id=:call"
                ),
                {
                    "p": saved["project_id"],
                    "run": saved["run_id"],
                    "call": f"extension_cb_{invocation_id.hex}_{sequence}",
                },
            )
        response.update(
            status="unknown", reason="connection_outcome_unconfirmed", waiting_invocation_id=str(nested_id)
        )
    except Exception:
        # Never reflect remote text, SQL, credentials or provider diagnostics into user code.
        response.update(status="denied", reason="connection_request_not_admitted")
    publish_json(response_path, response)
