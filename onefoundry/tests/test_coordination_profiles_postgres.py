"""Chief of Staff agent proposals, the operator tool ceiling, and inbox approval."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}


@contextmanager
def _env(tmp_path: Path) -> Iterator[tuple[TestClient, ToolGateway]]:
    root = tmp_path / f"secrets-{uuid4()}"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        coordination_mode="draft_only",
    )
    engine = create_engine(settings.database_url)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            yield client, ToolGateway(engine, "operator", live_text_settings=settings)
    finally:
        engine.dispose()


def _request(client: TestClient, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    response = client.request(method, path, json=body or {}, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _setup(client: TestClient, profile: str = "Chief of Staff") -> tuple[UUID, dict[str, str], UUID]:
    project = _request(client, "POST", "/api/projects", {"name": f"Agents {uuid4()}"})
    pid = UUID(project["id"])
    base = f"/api/projects/{pid}"
    profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
    _request(
        client, "PATCH", f"{base}/profiles/{profiles[profile]}", {"allowed_tools": ["propose_profile_v1"]}
    )
    _request(
        client,
        "PUT",
        f"{base}/coordination/tool-ceiling",
        {"tools": ["approved_memory_v1", "report_signal_v1"], "expected_version": 0},
    )
    order = _request(
        client,
        "POST",
        f"{base}/work-orders",
        {"title": "Plan the launch", "instructions": "Coordinate it.", "profile_id": profiles[profile]},
    )
    _request(client, "POST", f"{base}/work-orders/{order['id']}/ready")
    run = _request(client, "POST", f"{base}/work-orders/{order['id']}/runs")
    # Role tools run inside an in-flight model run; mark this manual run as one using the test owner login.
    owner = create_engine(Settings(db_user="onefoundry", db_password=os.environ["DB_PASSWORD"]).database_url)
    with owner.begin() as connection:
        connection.execute(
            text("UPDATE runs SET mode='live_text',tokens_used=NULL,cost_usd=NULL WHERE id=:id"),
            {"id": UUID(run["id"])},
        )
    owner.dispose()
    return pid, profiles, UUID(run["id"])


def _propose(
    gateway: ToolGateway, pid: UUID, run_id: UUID, profile_id: str, call: str, **changes: Any
) -> Any:
    arguments: dict[str, Any] = {
        "name": "Finance analyst",
        "instructions": "Model unit economics from supplied numbers.",
        "memory_scope": "project",
        "allowed_tools": ["approved_memory_v1"],
        "autonomy_level": "supervised",
        "first_work_order": {"title": "Price the pilot", "instructions": "Estimate margin at 50 seats."},
        "reason": "Pricing questions need a dedicated analyst.",
    }
    arguments.update(changes)
    return gateway.invoke_role_tool(
        pid, run_id, UUID(profile_id), call, "propose_profile_v1", arguments, tool_version=1
    )


def test_chief_proposal_waits_for_approval_and_creates_exactly_the_agent(tmp_path: Path) -> None:
    with _env(tmp_path) as (client, gateway):
        pid, profiles, run_id = _setup(client)
        base = f"/api/projects/{pid}"
        status = client.get(f"{base}/coordination").json()
        assert status["profile_tool_ceiling"] == ["approved_memory_v1", "report_signal_v1"]
        assert "propose_profile_v1" not in status["registered_tools"]
        receipt = _propose(gateway, pid, run_id, profiles["Chief of Staff"], "propose-1")
        assert receipt["result"]["status"] == "pending"
        assert "Finance analyst" not in {row["name"] for row in client.get(f"{base}/profiles").json()}
        approvals = client.get(f"{base}/approvals").json()
        approval = next(row for row in approvals if row["id"] == receipt["result"]["approval_id"])
        assert approval["status"] == "pending"
        assert "Create agent “Finance analyst”" in approval["content"]
        assert "Tools: approved_memory_v1" in approval["content"]
        _request(client, "POST", f"{base}/approvals/{approval['id']}/decision", {"decision": "approved"})
        created = next(
            row for row in client.get(f"{base}/profiles").json() if row["name"] == "Finance analyst"
        )
        assert created["kind"] == "custom"
        assert created["reports_to_profile_id"] == profiles["Chief of Staff"]
        assert created["allowed_tools"] == ["approved_memory_v1"]
        assert created["autonomy_level"] == "supervised"
        first = next(
            row for row in client.get(f"{base}/work-orders").json() if row["title"] == "Price the pilot"
        )
        assert first["status"] == "draft" and first["profile_id"] == created["id"]


def test_ceiling_role_and_rejection_boundaries(tmp_path: Path) -> None:
    with _env(tmp_path) as (client, gateway):
        pid, profiles, run_id = _setup(client)
        base = f"/api/projects/{pid}"
        with pytest.raises(ToolDenied, match="outside the operator's ceiling"):
            _propose(
                gateway,
                pid,
                run_id,
                profiles["Chief of Staff"],
                "propose-wide",
                allowed_tools=["public_web_search_v1"],
            )
        rejected = _propose(
            gateway, pid, run_id, profiles["Chief of Staff"], "propose-2", name="Support lead"
        )
        _request(
            client,
            "POST",
            f"{base}/approvals/{rejected['result']['approval_id']}/decision",
            {"decision": "rejected"},
        )
        assert "Support lead" not in {row["name"] for row in client.get(f"{base}/profiles").json()}

        narrowed = _propose(gateway, pid, run_id, profiles["Chief of Staff"], "propose-3", name="Ops lead")
        status = client.get(f"{base}/coordination").json()
        _request(
            client,
            "PUT",
            f"{base}/coordination/tool-ceiling",
            {"tools": [], "expected_version": status["version"]},
        )
        late = client.post(
            f"{base}/approvals/{narrowed['result']['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        )
        assert late.status_code == 409
        pending = next(
            row
            for row in client.get(f"{base}/approvals").json()
            if row["id"] == narrowed["result"]["approval_id"]
        )
        assert pending["status"] == "pending"
        bad = client.put(
            f"{base}/coordination/tool-ceiling",
            json={"tools": ["not_a_tool"], "expected_version": status["version"] + 1},
            headers=ORIGIN,
        )
        assert bad.status_code == 409

    with _env(tmp_path) as (client, gateway):
        pid, profiles, run_id = _setup(client, profile="Researcher")
        with pytest.raises(ToolDenied, match="only the Chief of Staff"):
            _propose(gateway, pid, run_id, profiles["Researcher"], "propose-4")
