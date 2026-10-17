"""Behavioral tests for isolated extension payload execution."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from uuid import uuid4

import pytest
from onefoundry_sdk import ExtensionManifest, build_package, inspect_package

from runner.extension_worker_child import execute


def _request(
    source: bytes,
    arguments: dict[str, object],
    resources: dict[str, bytes] | None = None,
) -> dict[str, object]:
    manifest = ExtensionManifest.model_validate(
        {
            "id": "worker-fixture",
            "version": "1.0.0",
            "name": "Worker fixture",
            "tools": [
                {
                    "id": "calculate",
                    "description": "Return a computed value",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    "returns": {
                        "type": "object",
                        "properties": {"answer": {"type": "integer"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "effect": "pure",
                    "runtime": "python",
                    "entrypoint": "calculator:calculate",
                }
            ],
        }
    )
    archive = build_package(manifest, {"src/calculator.py": source, **(resources or {})})
    identity = str(uuid4())
    return {
        "archive_base64": base64.b64encode(archive).decode(),
        "package_sha256": inspect_package(archive).package_sha256,
        "tool_id": "calculate",
        "execution_kind": "python",
        "arguments": arguments,
        "project_id": identity,
        "actor_id": "agent:qa",
        "run_id": identity,
        "profile_id": identity,
        "max_output_bytes": 1024,
    }


def test_python_tool_receives_only_restricted_context_and_schema_checked_result() -> None:
    request = _request(
        b"def calculate(context, value):\n"
        b"    assert context.actor_id == 'agent:qa'\n"
        b"    print('not a receipt')\n"
        b"    return {'answer': value + 1}\n",
        {"value": 4},
    )
    assert execute(request) == {"status": "success", "output": {"answer": 5}}


def test_python_subprocess_reads_packaged_read_only_resource() -> None:
    request = _request(
        b"from pathlib import Path\n"
        b"from stat import S_IMODE\n"
        b"import json\n"
        b"def calculate(context, value):\n"
        b"    resource = Path(__file__).resolve().parent.parent / 'resources' / 'delta.json'\n"
        b"    assert S_IMODE(resource.stat().st_mode) == 0o444\n"
        b"    delta = json.loads(resource.read_text(encoding='utf-8'))['delta']\n"
        b"    return {'answer': value + delta}\n",
        {"value": 4},
        {"resources/delta.json": b'{"delta":1}'},
    )
    result = subprocess.run(  # noqa: S603 - exact test interpreter and module
        [sys.executable, "-m", "runner.extension_worker_child"],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "kind": "result",
        "status": "success",
        "output": {"answer": 5},
    }


def test_invalid_arguments_rejected_before_payload_import() -> None:
    request = _request(b"raise AssertionError('payload imported')\n", {"value": "wrong"})
    assert execute(request) == {"status": "error", "error_code": "tool_input_invalid"}


def test_malformed_output_is_rejected_without_payload_details() -> None:
    request = _request(b"def calculate(context, value):\n    return {'answer': 'wrong'}\n", {"value": 1})
    assert execute(request) == {"status": "error", "error_code": "tool_output_invalid"}


def test_package_hash_mismatch_rejected_before_payload_import() -> None:
    request = _request(b"raise AssertionError('payload imported')\n", {"value": 1})
    request["package_sha256"] = "0" * 64
    assert execute(request) == {"status": "error", "error_code": "package_hash_mismatch"}


@pytest.mark.parametrize(
    ("callback_status", "expected"),
    [
        ("done", {"kind": "result", "status": "success", "output": {"answer": 5}}),
        ("unknown", {"kind": "result", "status": "error", "error_code": "connection_outcome_unknown"}),
    ],
)
def test_python_connection_uses_only_bounded_parent_pipe(
    callback_status: str, expected: dict[str, object]
) -> None:
    manifest = ExtensionManifest.model_validate(
        {
            "id": "callback-fixture",
            "version": "1.0.0",
            "name": "Callback fixture",
            "connections": [
                {
                    "id": "records",
                    "name": "Records",
                    "kind": "mcp",
                    "operations": [{"id": "lookup"}],
                }
            ],
            "tools": [
                {
                    "id": "calculate",
                    "description": "Use declared lookup",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                    "returns": {"type": "object", "properties": {"answer": {"type": "integer"}}},
                    "effect": "pure",
                    "runtime": "python",
                    "entrypoint": "calculator:calculate",
                    "required_connections": ["records"],
                }
            ],
        }
    )
    source = (
        b"async def calculate(context, value):\n"
        b"    found = await context.connections.invoke(\n"
        b"        slot='records', operation='lookup', arguments={'value': value})\n"
        b"    return {'answer': found['answer']}\n"
    )
    archive = build_package(manifest, {"src/calculator.py": source})
    identity = str(uuid4())
    request = {
        "archive_base64": base64.b64encode(archive).decode(),
        "package_sha256": inspect_package(archive).package_sha256,
        "tool_id": "calculate",
        "execution_kind": "python",
        "arguments": {"value": 4},
        "project_id": identity,
        "actor_id": "agent:qa",
        "run_id": identity,
        "profile_id": identity,
        "max_output_bytes": 1024,
        "job_sha256": "a" * 64,
    }
    with subprocess.Popen(  # noqa: S603 - exact test interpreter and module
        [sys.executable, "-m", "runner.extension_worker_child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        frame = json.loads(process.stdout.readline())
        assert frame == {
            "kind": "connection_request",
            "job_sha256": "a" * 64,
            "sequence": 1,
            "slot": "records",
            "operation": "lookup",
            "arguments": {"value": 4},
        }
        process.stdin.write(
            json.dumps(
                {
                    "kind": "connection_response",
                    "job_sha256": "a" * 64,
                    "sequence": 1,
                    "status": callback_status,
                    "result": {"answer": 5},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        result = json.loads(process.stdout.readline())
        assert result == expected
        process.stdin.close()
        assert process.wait(timeout=5) == 0
