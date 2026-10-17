"""Native plugin transport authenticates locally without packaging credentials."""

import importlib.util
import json
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).parents[1] / "plugins/onefoundry-company-memory/scripts/memory_bridge.py"
spec = importlib.util.spec_from_file_location("memory_bridge", SCRIPT)
assert spec and spec.loader
bridge: ModuleType = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def write_client(tmp_path: Path, port: int) -> tuple[Path, dict[str, Any]]:
    data = {
        "server_url": f"http://127.0.0.1:{port}/mcp",
        "api_key": secrets.token_urlsafe(32),
        "project_ids": [str(uuid4())],
        "timeout_seconds": 2,
        "max_request_bytes": 4096,
        "max_response_bytes": 4096,
    }
    path = tmp_path / "client.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, data


@pytest.mark.parametrize(
    "kind",
    [
        "public",
        "symlink",
        "remote_http",
        "credential_url",
        "extra",
        "bounds",
        "bad_key",
        "missing",
        "bad_project",
        "url_type",
        "url_control",
        "project_type",
    ],
)
def test_private_client_settings_fail_closed(tmp_path: Path, kind: str) -> None:
    path, data = write_client(tmp_path, 9877)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        actual = tmp_path / "actual.json"
        path.rename(actual)
        path.symlink_to(actual)
    elif kind == "missing":
        path.unlink()
    else:
        if kind == "remote_http":
            data["server_url"] = "http://outside.example.test/mcp"
        elif kind == "credential_url":
            data["server_url"] = "https://user:secret@example.test/mcp"
        elif kind == "extra":
            data["extra"] = True
        elif kind == "bounds":
            data["timeout_seconds"] = 0
        elif kind == "bad_key":
            data["api_key"] = "weak"
        elif kind == "url_type":
            data["server_url"] = 123
        elif kind == "url_control":
            data["server_url"] += "\n"
        elif kind == "project_type":
            data["project_ids"] = [123]
        else:
            data["project_ids"] = ["not-a-project"]
        path.write_text(json.dumps(data))
    with pytest.raises((OSError, ValueError)):
        bridge.configuration(path)


def test_real_stdio_to_http_transport_and_failure_identity(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    credential = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(request)
            if self.headers.get("Authorization") != "Bearer " + credential:
                self.send_response(401)
                self.end_headers()
                return
            if request["method"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "/must-never-be-called")
                self.end_headers()
                return
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            reply = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"tools": [], "instructions": "Fixture"},
            }
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path, data = write_client(tmp_path, server.server_port)
    data["api_key"] = credential
    path.write_text(json.dumps(data))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "redirect"},
    ]
    try:
        result = subprocess.run(  # noqa: S603 - real checked plugin entrypoint
            [sys.executable, str(SCRIPT), "--config", str(path)],
            input="".join(json.dumps(item) + "\n" for item in requests),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        assert [row["id"] for row in rows] == [1, 2, 3]
        assert data["project_ids"][0] in rows[0]["result"]["instructions"]
        assert rows[-1]["error"]["message"] == "memory_http_failed"
        assert credential not in result.stdout + result.stderr
        assert len(seen) == 4  # Redirect was not followed.
        data["api_key"] = secrets.token_urlsafe(32)
        path.write_text(json.dumps(data))
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(SCRIPT), "--config", str(path)],
            input=json.dumps(requests[2]) + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert json.loads(result.stdout)["error"]["message"] == "memory_authentication_failed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
