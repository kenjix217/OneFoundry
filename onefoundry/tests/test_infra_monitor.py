"""Infra snapshot semantics, including absent and stopped services."""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from onefoundry.infra_monitor import make_infra_router, read_status
from scripts.infra_snapshot import capture, parse_compose_output

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def write_snapshot(path: Path, *, age: int = 0, state: str = "running", health: str = "healthy") -> None:
    path.write_text(
        json.dumps(
            {
                "captured_at": (NOW - timedelta(seconds=age)).isoformat(),
                "services": [{"name": "db", "state": state, "health": health}],
            }
        )
    )


def test_read_status_running_and_stopped(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    write_snapshot(path)
    assert read_status(path, 60, NOW)["status"] == "healthy"
    write_snapshot(path, state="stopped", health="none")
    result = read_status(path, 60, NOW)
    assert result["status"] == "degraded"
    assert result["services"] == [{"name": "db", "state": "stopped", "health": "none"}]


def test_read_status_fails_closed_on_stale_missing_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    assert read_status(path, 60, NOW)["status"] == "unknown"
    write_snapshot(path, age=61)
    assert read_status(path, 60, NOW)["services"] == []
    path.write_text('{"captured_at": "invalid", "services": []}')
    assert read_status(path, 60, NOW)["status"] == "unknown"
    path.write_bytes(b"x" * 16_385)
    assert read_status(path, 60, NOW)["status"] == "unknown"


def test_compose_parser_marks_missing_and_stopped() -> None:
    output = "\n".join(
        [
            json.dumps({"Service": "app", "State": "running", "Health": ""}),
            json.dumps({"Service": "db", "State": "exited", "Health": ""}),
            json.dumps({"Service": "migrate", "State": "exited", "Health": ""}),
        ]
    )
    services = parse_compose_output(output, ["app", "db", "runner", "mailpit"])["services"]
    assert services == [
        {"name": "app", "state": "running", "health": "none"},
        {"name": "db", "state": "stopped", "health": "none"},
        {"name": "runner", "state": "missing", "health": "unknown"},
        {"name": "mailpit", "state": "missing", "health": "unknown"},
    ]


def test_capture_is_readable_by_non_owner_and_excludes_compose_fields(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n")
    output_path = tmp_path / "status.json"
    compose_output = json.dumps(
        {
            "Service": "db",
            "State": "running",
            "Health": "healthy",
            "Labels": "sensitive metadata",
            "Image": "private-image",
        }
    )
    with patch("scripts.infra_snapshot.subprocess.run", return_value=Mock(stdout=compose_output)):
        capture(compose_file, ["db"], output_path)
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o644
    assert "sensitive metadata" not in output_path.read_text()
    assert "private-image" not in output_path.read_text()
    assert json.loads(output_path.read_text())["services"] == [
        {"name": "db", "state": "running", "health": "healthy"}
    ]


def test_get_only_router_returns_current_status(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "services": [{"name": "runner", "state": "stopped", "health": "none"}],
            }
        )
    )
    app = FastAPI()
    app.include_router(make_infra_router(path, 60))
    with TestClient(app) as client:
        response = client.get("/api/infra/status")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert client.post("/api/infra/status").status_code == 405
