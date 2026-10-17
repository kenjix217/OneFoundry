"""One synthetic package across import, skills, connections, approvals, and isolation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION
from onefoundry_sdk import ExtensionManifest, build_package, inspect_package
from sqlalchemy import create_engine

from onefoundry import coordination_store
from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.gateway_extension_spool import prepare_spool, publish_json, read_json
from onefoundry.gateway_extension_worker_runtime import ExtensionWorkerRuntime, reconcile_once
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.skill_store import checked_procedures
from onefoundry.tool_gateway import ToolGateway
from tests.test_gateway_extension_remote_postgres import ORIGIN, _post

REFERENCE = Path(__file__).resolve().parents[1] / "sdk/python/examples/account-brief"


class _Fixture(BaseHTTPRequestHandler):
    api_calls: list[str] = []
    mcp_calls: list[str] = []
    deliveries: list[str] = []

    def _reply(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/accounts/acme":
            self._reply(404, {})
            return
        type(self).api_calls.append(self.path)
        self._reply(200, {"account": "Acme Example"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if self.path == "/deliver":
            brief = payload["brief"]
            type(self).deliveries.append(brief)
            self._reply(200, {"accepted": brief})
            return
        if self.path != "/mcp":
            self._reply(404, {})
            return
        method = payload["method"]
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "synthetic-account", "version": "1.0.0"},
            }
        elif method == "notifications/initialized":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup_account",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"account_id": {"type": "string"}},
                            "required": ["account_id"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            account_id = payload["params"]["arguments"]["account_id"]
            type(self).mcp_calls.append(account_id)
            result = {"structuredContent": {"account": "Acme Example"}}
        else:
            self._reply(400, {})
            return
        self._reply(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def fixture_origin() -> Iterator[str]:
    _Fixture.api_calls = []
    _Fixture.mcp_calls = []
    _Fixture.deliveries = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _reference_archive() -> bytes:
    manifest = ExtensionManifest.model_validate_json((REFERENCE / "extension.json").read_bytes())
    paths = (
        "src/brief.py",
        "skills/account-brief/SKILL.md",
        "tests/format_brief.json",
        "README.md",
    )
    return build_package(manifest, {name: (REFERENCE / name).read_bytes() for name in paths})


def _supervisor_once(settings: Settings, image_id: str, tmp_path: Path) -> None:
    config = tmp_path / "supervisor.json"
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(
            {
                "spool_root": str(prepare_spool(settings)),
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
    result = subprocess.run(  # noqa: S603 - fixed local supervisor and private config
        [sys.executable, "-m", "runner.extension_worker_supervisor", "--config", str(config), "--once"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def _signed(delivery: str, body: bytes, signing_key: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = hmac.new(
        signing_key.encode(), timestamp.encode() + b"." + delivery.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-OneFoundry-Timestamp": timestamp,
        "X-OneFoundry-Delivery": delivery,
        "X-OneFoundry-Signature": "sha256=" + digest,
    }


@pytest.mark.integration
def test_account_brief_package_joined_gateway_acceptance(
    tmp_path: Path, fixture_origin: str, monkeypatch: pytest.MonkeyPatch
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
        extension_allowed_origins=[fixture_origin],
        coordination_mode="draft_only",
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": f"Account brief {uuid4()}"})
            project_id = UUID(str(project["id"]))
            base = f"/api/projects/{project_id}"
            archive = _reference_archive()
            imported = cast(
                dict[str, Any],
                _post(
                    client,
                    f"{base}/extensions/packages/import",
                    {"format": "onefoundry", "archive_base64": base64.b64encode(archive).decode()},
                )["package"],
            )
            assert imported["state"] == "draft"
            assert imported["sha256"] == inspect_package(archive).package_sha256
            assert client.get(f"{base}/skills").json() == []
            package_id = imported["id"]

            # The imported slots have no endpoint, credential, or active binding.
            assert client.get(f"{base}/extensions/packages/{package_id}/bindings").json() == []
            connection_ids: list[str] = []
            for slot, endpoint in (
                ("account_api", fixture_origin),
                ("account_mcp", fixture_origin + "/mcp"),
                ("delivery", fixture_origin),
            ):
                connection = _post(
                    client,
                    f"{base}/extensions/connections",
                    {
                        "package_id": package_id,
                        "template_id": slot,
                        "label": f"Synthetic {slot}",
                        "config": {"transport": "streamable_http", "endpoint": endpoint},
                    },
                )
                assert connection["enabled"] is False
                connection_ids.append(str(connection["id"]))
                binding = client.put(
                    f"{base}/extensions/packages/{package_id}/bindings/{slot}",
                    json={"connection_id": connection["id"], "expected_version": 0},
                    headers=ORIGIN,
                )
                assert binding.status_code == 200, binding.text
            activation = _post(
                client,
                f"{base}/extensions/packages/{package_id}/activate",
                {
                    "expected_sha256": imported["sha256"],
                    "expected_version": "1.0.0",
                    "expected_revision": 1,
                },
            )
            assert client.get(f"{base}/skills").json() == []
            _post(client, f"{base}/approvals/{activation['approval_id']}/decision", {"decision": "approved"})
            assert client.get(f"{base}/extensions/packages/{package_id}").json()["state"] == "active"
            for connection_id in connection_ids:
                enabled = client.patch(
                    f"{base}/extensions/connections/{connection_id}",
                    json={"expected_version": 1, "enabled": True},
                    headers=ORIGIN,
                )
                assert enabled.status_code == 200, enabled.text

            ids = {
                name: canonical_tool_id("account-brief", name)
                for name in ("format_brief", "api_lookup", "mcp_lookup", "send_brief")
            }
            profile = next(
                row for row in client.get(f"{base}/profiles").json() if row["name"] == "Researcher"
            )
            profile_id = UUID(profile["id"])
            grant = client.patch(
                f"{base}/profiles/{profile_id}",
                json={"allowed_tools": list(ids.values())},
                headers=ORIGIN,
            )
            assert grant.status_code == 200, grant.text
            skill = next(
                row
                for row in client.get(f"{base}/skills").json()
                if row["name"] == "account-brief: Account brief"
            )
            pinned = client.put(
                f"{base}/profiles/{profile_id}/skills/{skill['id']}",
                json={"version": 1, "expected_binding_id": None},
                headers=ORIGIN,
            )
            assert pinned.status_code == 200, pinned.text
            with engine.connect() as db_connection:
                assert len(checked_procedures(db_connection, project_id, profile_id)) == 1

            order = _post(
                client,
                f"{base}/work-orders",
                {
                    "title": "Compose synthetic account brief",
                    "instructions": "Use synthetic Acme data",
                    "profile_id": str(profile_id),
                },
            )
            _post(client, f"{base}/work-orders/{order['id']}/ready", {})
            run_id = UUID(str(gateway.start_synthetic_text(project_id, UUID(str(order["id"])))["run_id"]))
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
            available = {item["id"] for item in gateway.available_role_tools(project_id, run_id, profile_id)}
            assert set(ids.values()) <= available

            for name, arguments, expected in (
                ("api_lookup", {"account_id": "acme"}, {"account": "Acme Example"}),
                ("mcp_lookup", {"account_id": "acme"}, {"account": "Acme Example"}),
            ):
                call_id = f"reference-{name}"
                pending = gateway.invoke_role_tool(
                    project_id, run_id, profile_id, call_id, ids[name], arguments, tool_version=1
                )
                assert pending["status"] == "approval_required"
                if name == "api_lookup":
                    assert _Fixture.api_calls == []
                assert _Fixture.mcp_calls == []
                _post(client, f"{base}/approvals/{pending['approval_id']}/decision", {"decision": "approved"})
                done = gateway.invoke_role_tool(
                    project_id, run_id, profile_id, call_id, ids[name], arguments, tool_version=1
                )
                assert done["status"] == "done" and done["result"] == expected
                replay = gateway.invoke_role_tool(
                    project_id, run_id, profile_id, call_id, ids[name], arguments, tool_version=1
                )
                assert replay["invocation_id"] == done["invocation_id"]
            assert _Fixture.api_calls == ["/accounts/acme"]
            assert _Fixture.mcp_calls == ["acme"]

            brief = "Acme Example: Synthetic renewal note"
            arguments = {"account": "Acme Example", "note": "Synthetic   renewal  note"}
            pending = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "reference-format",
                ids["format_brief"],
                arguments,
                tool_version=1,
            )
            assert pending["status"] == "running"
            invocation_id = UUID(pending["invocation_id"])
            job = read_json(root / "jobs" / f"{invocation_id}.json", 7 * 1024 * 1024)
            assert job["worker_image"] == image_id
            _supervisor_once(settings, image_id, tmp_path)
            receipt_path = root / "receipts" / f"{invocation_id}.json"
            receipt = read_json(receipt_path, 131072)
            assert receipt["status"] == "done" and receipt["result"] == {"brief": brief}
            assert receipt["job_sha256"] == job["job_sha256"]
            receipt_bytes = receipt_path.read_bytes()
            receipt_inode = receipt_path.stat().st_ino
            reconcile_once(gateway)
            done = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "reference-format",
                ids["format_brief"],
                arguments,
                tool_version=1,
            )
            assert done["status"] == "done" and done["result"] == {"brief": brief}
            # A second supervisor pass must not create a second effect or receipt.
            (tmp_path / "supervisor.json").unlink()
            _supervisor_once(settings, image_id, tmp_path)
            assert receipt_path.read_bytes() == receipt_bytes
            assert receipt_path.stat().st_ino == receipt_inode

            outbound = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "reference-delivery",
                ids["send_brief"],
                {"brief": brief},
                tool_version=1,
            )
            assert outbound["status"] == "approval_required"
            assert _Fixture.deliveries == []
            _post(client, f"{base}/approvals/{outbound['approval_id']}/decision", {"decision": "approved"})
            delivered = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "reference-delivery",
                ids["send_brief"],
                {"brief": brief},
                tool_version=1,
            )
            assert delivered["status"] == "done" and delivered["result"] == {"accepted": brief}
            replay = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "reference-delivery",
                ids["send_brief"],
                {"brief": brief},
                tool_version=1,
            )
            assert replay["invocation_id"] == delivered["invocation_id"]
            assert _Fixture.deliveries == [brief]

            signing_key = secrets.token_urlsafe(32)
            webhook = _post(
                client,
                f"{base}/extensions/webhooks",
                {"name": "Synthetic account", "signing_secret": signing_key},
            )
            assert webhook["enabled"] is False
            enabled = client.patch(
                f"{base}/extensions/webhooks/{webhook['id']}",
                json={"expected_version": webhook["version"], "enabled": True},
                headers=ORIGIN,
            )
            assert enabled.status_code == 200, enabled.text
            sales = next(row for row in client.get(f"{base}/profiles").json() if row["name"] == "Sales")
            _post(
                client,
                f"{base}/coordination/rules",
                {
                    "name": "New synthetic account to draft",
                    "signal_type": "webhook.received",
                    "filters": {"event_type": "account.created"},
                    "target_profile_id": sales["id"],
                    "title": "Review synthetic account",
                    "instructions": "Prepare an account brief draft for review.",
                },
            )
            body = json.dumps({"event_type": "account.created", "account_id": "acme"}).encode()
            headers = _signed("account-delivery-1", body, signing_key)
            url = f"/api/extensions/webhooks/{webhook['id']}"
            received = client.post(url, content=body, headers=headers)
            assert received.status_code == 202 and received.json()["duplicate"] is False
            duplicate = client.post(url, content=body, headers=headers)
            assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
            coordinator = ToolGateway(
                engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings
            )
            for _ in range(8):
                if not coordination_store.run_once(coordinator, settings, project_id):
                    break
            orders = client.get(f"{base}/work-orders").json()
            drafts = [row for row in orders if row["title"] == "Review synthetic account"]
            assert len(drafts) == 1 and drafts[0]["status"] == "draft"
    finally:
        engine.dispose()
