"""Joined API, Tool Gateway, external supervisor, Docker child, and receipt proof."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.gateway_extension_spool import prepare_spool, publish_json, read_json
from onefoundry.gateway_extension_worker_runtime import ExtensionWorkerRuntime, reconcile_once
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_gateway_extension_remote_postgres import ORIGIN, _post


@pytest.mark.integration
def test_api_to_isolated_supervisor_to_exact_gateway_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_id = os.environ.get("ONEFOUNDRY_EXTENSION_TEST_IMAGE")
    if image_id is None:
        pytest.skip("exact disposable Docker worker image ID not configured")

    async def manual_reconciliation(_runtime: ExtensionWorkerRuntime) -> None:
        return None

    monkeypatch.setattr(ExtensionWorkerRuntime, "start", manual_reconciliation)
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        runner_spool_root=str(tmp_path / "spool"),
        extension_worker_image=image_id,
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": f"Worker E2E {uuid4()}"})
            project_id = UUID(str(project["id"]))
            base = f"/api/projects/{project_id}"
            package = _post(
                client,
                f"{base}/extensions/packages",
                {
                    "manifest": {
                        "schema_version": 1,
                        "id": "pure-worker-e2e",
                        "version": "1.0.0",
                        "name": "Pure worker E2E",
                        "tools": [
                            {
                                "id": "calculate",
                                "description": "Calculate one exact value",
                                "effect": "pure",
                                "runtime": "python",
                                "entrypoint": "calculate:run",
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
                            }
                        ],
                    },
                    "files": {
                        "src/calculate.py": "def run(context, value):\n    return {'answer': value + 1}\n"
                    },
                },
            )
            review = _post(
                client,
                f"{base}/extensions/packages/{package['id']}/activate",
                {
                    "expected_sha256": package["sha256"],
                    "expected_version": "1.0.0",
                    "expected_revision": 1,
                },
            )
            _post(client, f"{base}/approvals/{review['approval_id']}/decision", {"decision": "approved"})
            profile = next(
                item for item in client.get(f"{base}/profiles").json() if item["name"] == "Researcher"
            )
            profile_id = UUID(profile["id"])
            tool_id = canonical_tool_id("pure-worker-e2e", "calculate")
            granted = client.patch(
                f"{base}/profiles/{profile_id}",
                json={"allowed_tools": [tool_id]},
                headers=ORIGIN,
            )
            assert granted.status_code == 200, granted.text
            order = _post(
                client,
                f"{base}/work-orders",
                {"title": "Pure worker E2E", "instructions": "Calculate", "profile_id": str(profile_id)},
            )
            _post(client, f"{base}/work-orders/{order['id']}/ready", {})
            run = gateway.start_synthetic_text(project_id, UUID(str(order["id"])))
            run_id = UUID(str(run["run_id"]))
            root = prepare_spool(settings)
            publish_json(
                root / "heartbeat.json",
                {
                    "schema_version": 1,
                    "image_id": image_id,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "supervisor_id": str(uuid4()),
                    "memory_mb": settings.extension_worker_memory_mb,
                    "processes": settings.extension_worker_processes,
                    "cpus": settings.extension_worker_cpus,
                    "max_timeout_seconds": settings.extension_timeout_seconds,
                    "max_output_bytes": settings.extension_worker_max_output_bytes,
                },
            )
            pending = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "pure-worker-e2e-call",
                tool_id,
                {"value": 4},
                tool_version=1,
            )
            assert pending["status"] == "running"
            invocation_id = UUID(pending["invocation_id"])
            job = read_json(root / "jobs" / f"{invocation_id}.json", 7 * 1024 * 1024)
            assert job["worker_image"] == image_id
            assert job["arguments"] == {"value": 4}

            config = tmp_path / "supervisor.json"
            descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {
                        "spool_root": str(root),
                        "image_id": image_id,
                        "docker_binary": "/usr/bin/docker",
                        "docker_socket": "/var/run/docker.sock",
                        "memory_mb": settings.extension_worker_memory_mb,
                        "processes": settings.extension_worker_processes,
                        "cpus": settings.extension_worker_cpus,
                        "max_timeout_seconds": settings.extension_timeout_seconds,
                        "max_output_bytes": settings.extension_worker_max_output_bytes,
                        "poll_seconds": 1,
                    },
                    output,
                )

            command = (
                sys.executable,
                "-m",
                "runner.extension_worker_supervisor",
                "--config",
                str(config),
                "--once",
            )
            first = subprocess.run(  # noqa: S603 - exact local supervisor module and private config
                command, check=False, capture_output=True, text=True, timeout=60
            )
            assert first.returncode == 0, first.stderr
            receipt_path = root / "receipts" / f"{invocation_id}.json"
            exact_receipt = read_json(receipt_path, 131072)
            assert exact_receipt == {
                "schema_version": 1,
                "invocation_id": str(invocation_id),
                "package_sha256": job["package_sha256"],
                "job_sha256": job["job_sha256"],
                "status": "done",
                "result": {"answer": 5},
            }
            receipt_bytes = receipt_path.read_bytes()
            receipt_inode = receipt_path.stat().st_ino
            claim = root / "claims" / f"{invocation_id}.json"
            assert claim.read_text() == job["job_sha256"]

            reconcile_once(gateway)
            done = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "pure-worker-e2e-call",
                tool_id,
                {"value": 4},
                tool_version=1,
            )
            assert done["status"] == "done"
            assert done["result"] == {"answer": 5}
            with gateway.engine.connect() as connection:
                saved = connection.execute(
                    text("SELECT status,result FROM extension_invocations WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": invocation_id},
                ).one()
            assert saved.status == "done"
            assert saved.result == {"answer": 5}

            second = subprocess.run(  # noqa: S603 - exact local supervisor module and private config
                command, check=False, capture_output=True, text=True, timeout=30
            )
            assert second.returncode == 0, second.stderr
            reconcile_once(gateway)
            assert receipt_path.read_bytes() == receipt_bytes
            assert receipt_path.stat().st_ino == receipt_inode
            assert claim.read_text() == job["job_sha256"]
            print(
                f"joined extension E2E: invocation={invocation_id} image={image_id} "
                f"job={job['job_sha256']} receipt=done result={{'answer': 5}} no_replay=true"
            )
    finally:
        engine.dispose()
