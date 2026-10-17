"""Container-only Python tool entrypoint. No host service, credential, or network access."""

from __future__ import annotations

import asyncio
import base64
import importlib
import io
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from onefoundry_sdk import inspect_package

MAX_STDIN_BYTES = 6 * 1024 * 1024


class ConnectionCallError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RestrictedToolContext:
    def __init__(
        self, project_id: str, actor_id: str, run_id: str, profile_id: str, job_sha256: str = ""
    ) -> None:
        self.project_id = project_id
        self.actor_id = actor_id
        self.run_id = run_id
        self.profile_id = profile_id
        self.connections = _ConnectionInvoker(job_sha256)


class _ConnectionInvoker:
    def __init__(self, job_sha256: str) -> None:
        self.job_sha256 = job_sha256
        self.sequence = 0

    async def invoke(self, *, slot: str, operation: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.job_sha256 or not isinstance(arguments, dict):
            raise RuntimeError("connection_request_invalid")
        self.sequence += 1
        request = {
            "kind": "connection_request",
            "job_sha256": self.job_sha256,
            "sequence": self.sequence,
            "slot": slot,
            "operation": operation,
            "arguments": arguments,
        }
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise RuntimeError("connection_request_too_large")
        output_stream, input_stream = sys.__stdout__, sys.__stdin__
        if output_stream is None or input_stream is None:
            raise ConnectionCallError("connection_channel_unavailable")
        output_stream.write(encoded + "\n")
        output_stream.flush()
        line = input_stream.readline(65537)
        if not line or len(line.encode()) > 65536:
            raise RuntimeError("connection_response_unavailable")
        response = json.loads(line)
        if (
            not isinstance(response, dict)
            or response.get("kind") != "connection_response"
            or response.get("sequence") != self.sequence
            or response.get("job_sha256") != self.job_sha256
        ):
            raise RuntimeError("connection_response_invalid")
        status = response.get("status")
        if status != "done":
            code = {
                "approval_required": "connection_approval_required",
                "denied": "connection_denied",
                "failed": "connection_failed",
                "unknown": "connection_outcome_unknown",
            }.get(status if isinstance(status, str) else "", "connection_response_invalid")
            raise ConnectionCallError(code)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("connection_result_invalid")
        return result


def execute(request: Mapping[str, Any]) -> dict[str, Any]:
    archive = base64.b64decode(str(request["archive_base64"]), validate=True)
    package = inspect_package(archive)
    if package.package_sha256 != request["package_sha256"]:
        return {"status": "error", "error_code": "package_hash_mismatch"}
    tool_id = str(request["tool_id"])
    tool = next((candidate for candidate in package.manifest.tools if candidate.id == tool_id), None)
    kind = request.get("execution_kind")
    if tool is None or kind not in {"python", "mcp_stdio"}:
        return {"status": "error", "error_code": "tool_not_declared"}
    if kind == "python" and (tool.runtime != "python" or tool.entrypoint is None):
        return {"status": "error", "error_code": "python_tool_not_declared"}
    if kind == "mcp_stdio" and tool.runtime != "mcp":
        return {"status": "error", "error_code": "mcp_tool_not_declared"}
    arguments = request["arguments"]
    if not isinstance(arguments, dict):
        return {"status": "error", "error_code": "arguments_not_object"}
    input_errors = list(Draft202012Validator(tool.parameters).iter_errors(arguments))
    if input_errors:
        return {"status": "error", "error_code": "tool_input_invalid"}
    with tempfile.TemporaryDirectory(prefix="onefoundry-tool-") as temporary:
        source_root = Path(temporary) / "src"
        source_root.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive)) as payload:
            for member in payload.infolist():
                if not member.filename.startswith(("src/", "resources/")):
                    continue
                destination = Path(temporary) / member.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload.read(member))
                if member.filename.startswith("resources/"):
                    destination.chmod(0o444)
        if kind == "mcp_stdio":
            output = asyncio.run(_call_mcp_stdio(request, Path(temporary), arguments))
        else:
            assert tool.entrypoint is not None
            module_name, function_name = tool.entrypoint.split(":", 1)
            sys.path.insert(0, str(source_root))
            try:
                sys.modules.pop(module_name, None)
                context = RestrictedToolContext(
                    str(request["project_id"]),
                    str(request["actor_id"]),
                    str(request["run_id"]),
                    str(request["profile_id"]),
                    str(request.get("job_sha256", "")),
                )
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    function = getattr(importlib.import_module(module_name), function_name)
                    output = function(context, **arguments)
                    if asyncio.iscoroutine(output):
                        output = asyncio.run(output)
            finally:
                sys.path.remove(str(source_root))
                for name, loaded in tuple(sys.modules.items()):
                    loaded_path = getattr(loaded, "__file__", None)
                    if isinstance(loaded_path, str) and Path(loaded_path).is_relative_to(source_root):
                        del sys.modules[name]
    if not isinstance(output, dict):
        return {"status": "error", "error_code": "tool_output_not_object"}
    output_errors = list(Draft202012Validator(tool.returns).iter_errors(output))
    if output_errors:
        return {"status": "error", "error_code": "tool_output_invalid"}
    serialized = json.dumps(
        output, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if len(serialized) > int(request["max_output_bytes"]):
        return {"status": "error", "error_code": "tool_output_too_large"}
    return {"status": "success", "output": output}


async def _call_mcp_stdio(request: Mapping[str, Any], package_root: Path, arguments: dict[str, Any]) -> Any:
    module = str(request["stdio_module"])
    path = package_root / module
    if not path.is_file() or path.is_symlink():
        raise ValueError("mcp_module_unavailable")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(path)],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},  # noqa: S108
        cwd=str(path.parent),
    )
    async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        result = await session.call_tool(str(request["operation"]), arguments)
    if result.is_error:
        raise ValueError("mcp_tool_error")
    return result.structured_content


def main() -> int:
    try:
        data = sys.stdin.buffer.readline(MAX_STDIN_BYTES + 1)
        if len(data) > MAX_STDIN_BYTES:
            result = {"status": "error", "error_code": "request_too_large"}
        else:
            request = json.loads(data)
            result = (
                execute(request)
                if isinstance(request, dict)
                else {"status": "error", "error_code": "request_invalid"}
            )
    except ConnectionCallError as error:
        result = {"status": "error", "error_code": error.code}
    except Exception:  # noqa: BLE001 - omit untrusted exception text from receipt
        result = {"status": "error", "error_code": "tool_execution_failed"}
    sys.stdout.write(json.dumps({"kind": "result", **result}, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    raise SystemExit(main())
