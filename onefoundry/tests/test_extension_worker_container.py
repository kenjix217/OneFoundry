"""Real Docker proof for extension payload execution and forbidden host access."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from onefoundry_sdk import ExtensionManifest, build_package, inspect_package

from runner.extension_fixture_test import run_fixtures
from runner.extension_worker_host import WorkerPolicy, canonical_job_sha256, run_job


@pytest.mark.integration
def test_isolated_python_boundary_and_result() -> None:
    image = os.environ.get("ONEFOUNDRY_EXTENSION_TEST_IMAGE")
    if not image:
        pytest.skip("disposable worker image ID not configured")
    policy = WorkerPolicy(
        image_id=image,
        docker_binary=Path("/usr/bin/docker"),
        docker_socket=Path("/var/run/docker.sock"),
        memory_mb=256,
        processes=16,
        cpus=1,
        max_timeout_seconds=20,
        max_output_bytes=4096,
    )
    manifest = ExtensionManifest.model_validate(
        {
            "id": "isolation-fixture",
            "version": "1.0.0",
            "name": "Isolation fixture",
            "tools": [
                {
                    "id": "probe",
                    "description": "Probe container isolation",
                    "parameters": {"type": "object", "properties": {}},
                    "returns": {
                        "type": "object",
                        "properties": {
                            "host_paths_hidden": {"type": "boolean"},
                            "network_blocked": {"type": "boolean"},
                            "root_read_only": {"type": "boolean"},
                        },
                        "required": ["host_paths_hidden", "network_blocked", "root_read_only"],
                    },
                    "effect": "pure",
                    "runtime": "python",
                    "entrypoint": "probe:run",
                }
            ],
        }
    )
    source = b"""\
import os
import socket
def run(context):
    paths = ('/var/run/docker.sock', '/runner-spool', '/run/secrets')
    hidden = all(not os.path.exists(path) for path in paths)
    network = False
    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect(('1.1.1.1', 443))
    except OSError:
        network = True
    finally:
        sock.close()
    root_read_only = False
    try:
        with open('/escape-probe', 'w') as output:
            output.write('x')
    except OSError:
        root_read_only = True
    return {'host_paths_hidden': hidden, 'network_blocked': network, 'root_read_only': root_read_only}
"""
    archive = build_package(manifest, {"src/probe.py": source})
    identity = str(uuid4())
    job: dict[str, object] = {
        "schema_version": 1,
        "invocation_id": identity,
        "project_id": identity,
        "actor_id": "agent:qa",
        "run_id": identity,
        "profile_id": identity,
        "package_sha256": inspect_package(archive).package_sha256,
        "archive_base64": base64.b64encode(archive).decode(),
        "tool_id": "probe",
        "arguments": {},
        "timeout_seconds": 10,
        "max_output_bytes": 4096,
        "worker_image": image,
        "memory_mb": 256,
        "processes": 16,
        "cpus": 1,
        "execution_kind": "python",
    }
    job["job_sha256"] = canonical_job_sha256(job)
    receipt = run_job(job, policy)
    assert receipt["status"] == "done", receipt.get("reason", receipt)
    assert receipt["result"] == {
        "host_paths_hidden": True,
        "network_blocked": True,
        "root_read_only": True,
    }


@pytest.mark.integration
def test_pinned_stdio_mcp_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    image = os.environ.get("ONEFOUNDRY_EXTENSION_TEST_IMAGE")
    if not image:
        pytest.skip("disposable worker image ID not configured")
    policy = WorkerPolicy(
        image_id=image,
        docker_binary=Path("/usr/bin/docker"),
        docker_socket=Path("/var/run/docker.sock"),
        memory_mb=256,
        processes=16,
        cpus=1,
        max_timeout_seconds=20,
        max_output_bytes=4096,
    )
    output_schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    manifest = ExtensionManifest.model_validate(
        {
            "id": "stdio-fixture",
            "version": "1.0.0",
            "name": "Stdio fixture",
            "connections": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "mcp",
                    "operations": [{"id": "lookup"}],
                }
            ],
            "tools": [
                {
                    "id": "lookup",
                    "description": "Lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    "returns": output_schema,
                    "effect": "read",
                    "runtime": "mcp",
                    "connection": "local",
                    "operation": "lookup",
                }
            ],
        }
    )
    source = b"""\
from mcp.server.mcpserver import MCPServer
mcp = MCPServer('fixture')
@mcp.tool()
def lookup(value: int) -> dict[str, int]:
    return {'answer': value + 1}
if __name__ == '__main__':
    mcp.run(transport='stdio')
"""
    archive = build_package(manifest, {"src/server.py": source})
    identity = str(uuid4())
    job: dict[str, object] = {
        "schema_version": 1,
        "invocation_id": identity,
        "project_id": identity,
        "actor_id": "agent:qa",
        "run_id": identity,
        "profile_id": identity,
        "package_sha256": inspect_package(archive).package_sha256,
        "archive_base64": base64.b64encode(archive).decode(),
        "tool_id": "lookup",
        "arguments": {"value": 4},
        "timeout_seconds": 15,
        "max_output_bytes": 4096,
        "worker_image": image,
        "memory_mb": 256,
        "processes": 16,
        "cpus": 1,
        "execution_kind": "mcp_stdio",
        "stdio_module": "src/server.py",
        "operation": "lookup",
        "expected_schema": output_schema,
    }
    job["job_sha256"] = canonical_job_sha256(job)
    from runner import extension_worker_host as host

    commands: list[str] = []
    original_docker = host._docker

    def observed_docker(policy: WorkerPolicy, *args: str, **kwargs: object) -> str:
        commands.append(args[0])
        return original_docker(policy, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host, "_docker", observed_docker)
    receipt = run_job(job, policy)
    assert receipt["status"] == "done", (receipt.get("reason", receipt), commands)
    assert receipt["result"] == {"answer": 5}


@pytest.mark.integration
def test_isolated_behavioral_fixture_driver() -> None:
    image = os.environ.get("ONEFOUNDRY_EXTENSION_TEST_IMAGE")
    if not image:
        pytest.skip("disposable worker image ID not configured")
    policy = WorkerPolicy(
        image_id=image,
        docker_binary=Path("/usr/bin/docker"),
        docker_socket=Path("/var/run/docker.sock"),
        memory_mb=256,
        processes=16,
        cpus=1,
        max_timeout_seconds=20,
        max_output_bytes=4096,
    )
    manifest = ExtensionManifest.model_validate(
        {
            "id": "fixture-driver",
            "version": "1.0.0",
            "name": "Fixture driver",
            "tools": [
                {
                    "id": "calculate",
                    "description": "Calculate",
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
    archive = build_package(
        manifest,
        {
            "src/calculator.py": (
                b"from pathlib import Path\n"
                b"from stat import S_IMODE\n"
                b"import json\n"
                b"def calculate(context, value):\n"
                b"    resource = Path(__file__).resolve().parent.parent / 'resources' / 'delta.json'\n"
                b"    assert S_IMODE(resource.stat().st_mode) == 0o444\n"
                b"    return {'answer': value + json.loads(resource.read_text())['delta']}\n"
            ),
            "resources/delta.json": b'{"delta":1}',
            "tests/calculate.json": json.dumps(
                {"tool_id": "calculate", "input": {"value": 4}, "expected_output": {"answer": 5}}
            ).encode(),
        },
    )
    report = run_fixtures(archive, policy)
    assert report["fixtures"] == report["passed"] == 1, report
    assert report["failures"] == []
