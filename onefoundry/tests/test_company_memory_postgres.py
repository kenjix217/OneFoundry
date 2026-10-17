"""Real PostgreSQL review, replacement, withdrawal, provenance, and race checks."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.company_memory_schemas import CaptureCreate, MemoryTransition
from onefoundry.gateway_errors import Conflict
from onefoundry.main import create_app
from onefoundry.memory_plugin_auth import MemoryTokenVerifier
from onefoundry.memory_plugin_gateway import MemoryPluginGateway
from onefoundry.memory_plugin_service import create_memory_plugin_app
from onefoundry.memory_plugin_settings import MemoryGrant
from onefoundry.memory_schemas import CandidateReview
from onefoundry.settings import Settings
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.tool_gateway import ToolGateway
from tests.memory_plugin_fixtures import keypair, policy, token
from tests.test_task_session_postgres import _order, _settings

ORIGIN = {"origin": "http://localhost:8765"}


def capture(summary: str, scope: str = "project", **extra: Any) -> CaptureCreate:
    return CaptureCreate.model_validate(
        {
            "client_capture_id": uuid4(),
            "source_ref": "Operator-provided company excerpt",
            "authorized_company_content": True,
            "messages": [{"speaker": "user", "text": summary, "sent_at": datetime.now(UTC).isoformat()}],
            "summary": summary,
            "scope": scope,
            **extra,
        }
    )


def owner() -> ToolGateway:
    return ToolGateway(
        create_engine(
            Settings(
                db_user=os.environ["APP_DB_USER"], db_password=os.environ["APP_DB_PASSWORD"]
            ).database_url
        ),
        "operator",
    )


def publish(gateway: ToolGateway, project: UUID, data: CaptureCreate) -> dict[str, Any]:
    receipt = gateway.capture_company_memory(project, data)
    gateway.review_candidate(project, receipt["candidate"]["id"], CandidateReview(decision="approved"))
    return next(
        item
        for item in gateway.retrieve_memory(project, "", 50)
        if item["source_candidate_id"] == receipt["candidate"]["id"]
    )


def test_capture_review_correction_withdrawal_and_source_archive(tmp_path: Path) -> None:
    term = f"copper{uuid4().hex}"
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = UUID(
            client.post("/api/projects", json={"name": f"{term} company memory"}, headers=ORIGIN).json()["id"]
        )
        other = UUID(client.post("/api/projects", json={"name": "Unrelated"}, headers=ORIGIN).json()["id"])
        gateway = owner()
        path = f"/api/projects/{project}"
        data = capture(f"{term} schedulemarker Tuesday", "company")
        response = client.post(f"{path}/memory/captures", json=data.model_dump(mode="json"), headers=ORIGIN)
        assert response.status_code == 201, response.text
        receipt = response.json()
        assert receipt["candidate"]["status"] == "pending" and receipt["result"]["run_id"] is None
        assert receipt["result"]["source_kind"] == "conversation_capture"
        assert gateway.retrieve_memory(project, f"{term}", 20) == []
        archive = client.get(f"{path}/memory/captures/{receipt['source_id']}").json()
        assert archive["messages"][0]["speaker"] == "user"
        assert archive["messages"][0]["sent_at"] and archive["created_at"]
        assert archive["actor_id"] == "operator" and archive["source_sha256"] == receipt["source_sha256"]
        assert client.get(f"/api/projects/{other}/memory/captures/{receipt['source_id']}").status_code == 404
        assert (
            client.post(
                f"{path}/candidates/{receipt['candidate']['id']}/review",
                json={"decision": "approved"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        old = gateway.retrieve_memory(project, f"{term}", 20)[0]
        proposal = gateway.capture_company_memory(
            project, capture(f"{term} schedulemarker Wednesday", "company", replaces_memory_id=old["id"])
        )
        assert gateway.retrieve_memory(project, f"{term}", 20)[0]["text"] == f"{term} schedulemarker Tuesday"
        gateway.review_candidate(project, proposal["candidate"]["id"], CandidateReview(decision="approved"))
        current = gateway.retrieve_memory(other, f"{term}", 20)[0]
        assert current["text"] == f"{term} schedulemarker Wednesday"
        historical = gateway.read_memory(project, old["id"], include_history=True)
        assert historical["state"] == "superseded" and historical["superseded_by"] == current["id"]
        assert (
            historical["text"] == f"{term} schedulemarker Tuesday"
            and historical["changed_by_actor_id"] == "operator"
        )
        assert client.get(f"{path}/memory/{old['id']}").status_code == 404
        assert client.get(f"{path}/memory/{old['id']}?include_history=true").json()["state"] == "superseded"
        assert (
            client.post(
                f"{path}/memory/{old['id']}/state",
                json={"action": "restore", "reason": "mistake"},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"{path}/memory/{current['id']}/state",
                json={"action": "withdraw", "reason": "Review date again"},
                headers=ORIGIN,
            ).json()["state"]
            == "withdrawn"
        )
        assert gateway.retrieve_memory(project, f"{term}", 20) == []
        assert (
            client.post(
                f"{path}/memory/{current['id']}/state",
                json={"action": "restore", "reason": "Confirmed by operator"},
                headers=ORIGIN,
            ).json()["state"]
            == "current"
        )
        assert len(gateway.retrieve_memory(project, f"{term}", 20)) == 1
        assert client.get(f"{path}/memory/captures/{receipt['source_id']}").json() == archive
        for invalid in (capture("Bad other project", "company", replaces_memory_id=current["id"]),):
            assert (
                client.post(
                    f"/api/projects/{other}/memory/captures",
                    json=invalid.model_dump(mode="json"),
                    headers=ORIGIN,
                ).status_code
                == 404
            )
        assert (
            client.post(
                f"{path}/memory/captures",
                json=capture("Bad scope", replaces_memory_id=current["id"]).model_dump(mode="json"),
                headers=ORIGIN,
            ).status_code
            == 409
        )
        with gateway.engine.connect() as connection:
            payloads: Any = (
                connection.execute(text("SELECT payload FROM events WHERE project_id=:p"), {"p": project})
                .scalars()
                .all()
            )
            assert f"{term} schedulemarker" not in json.dumps(payloads)
        with gateway.engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(text("DELETE FROM memory_captures WHERE project_id=:p"), {"p": project})
        gateway.engine.dispose()


def test_capture_retry_race_and_review_serialization(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        project = UUID(
            client.post("/api/projects", json={"name": "Memory races"}, headers=ORIGIN).json()["id"]
        )
        gateway = owner()
        data = capture("Copper reviewed fact")
        with ThreadPoolExecutor(max_workers=2) as workers:
            receipts = list(workers.map(lambda _: gateway.capture_company_memory(project, data), range(2)))
        assert receipts[0] == receipts[1]
        with pytest.raises(Conflict):
            gateway.capture_company_memory(project, data.model_copy(update={"summary": "Changed fact"}))
        candidate = receipts[0]["candidate"]["id"]

        def review(_: int) -> str:
            try:
                gateway.review_candidate(project, candidate, CandidateReview(decision="approved"))
                return "approved"
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as workers:
            assert sorted(workers.map(review, range(2))) == ["approved", "conflict"]
        old = gateway.retrieve_memory(project, "Copper", 20)[0]
        first = gateway.capture_company_memory(
            project, capture("Copper first correction", replaces_memory_id=old["id"])
        )
        second = gateway.capture_company_memory(
            project, capture("Copper second correction", replaces_memory_id=old["id"])
        )
        gateway.review_candidate(project, first["candidate"]["id"], CandidateReview(decision="approved"))
        with pytest.raises(Conflict):
            gateway.review_candidate(project, second["candidate"]["id"], CandidateReview(decision="approved"))
        assert (
            next(
                c for c in gateway.list_candidates(project, "pending") if c["id"] == second["candidate"]["id"]
            )["status"]
            == "pending"
        )
        gateway.engine.dispose()


def test_capture_rolls_back_when_event_owner_fails(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        project = UUID(
            client.post("/api/projects", json={"name": "Memory failure"}, headers=ORIGIN).json()["id"]
        )
        gateway = owner()
        with patch.object(gateway, "_event", side_effect=RuntimeError("event owner failed")):
            with pytest.raises(RuntimeError, match="event owner failed"):
                gateway.capture_company_memory(project, capture("Copper failed source"))
        assert gateway.list_candidates(project, None) == []
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM memory_captures WHERE project_id=:p"), {"p": project}
                )
                == 0
            )
        gateway.engine.dispose()


def test_real_mcp_shares_authoritative_store_and_hides_sources(tmp_path: Path) -> None:
    term = f"copper{uuid4().hex}"
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        project = UUID(client.post("/api/projects", json={"name": "MCP store"}, headers=ORIGIN).json()["id"])
        other = UUID(client.post("/api/projects", json={"name": "Other store"}, headers=ORIGIN).json()["id"])
        gateway = owner()
        current = publish(gateway, project, capture(f"{term} authoritative approved fact"))
        publish(gateway, other, capture(f"{term} company visibility", "company"))
        publish(gateway, other, capture(f"{term} unrelated private fact"))
        grant = MemoryGrant(
            subject="synthetic-operator",
            client_id="synthetic-dot",
            actor_id="external-memory:dot",
            project_ids=(project,),
            company_read=True,
            capture=True,
        )
        configured = policy(grants=(grant,))
        external = ToolGateway(gateway.engine, grant.actor_id)
        facade = MemoryPluginGateway(configured, {grant.actor_id: external})
        private, public = keypair()
        headers = {
            "authorization": f"Bearer {token(private, configured, scope='memory.read memory.capture')}",
            "accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        app = create_memory_plugin_app(configured, facade, MemoryTokenVerifier(configured, public))
        with TestClient(app, base_url="https://memory.example.test", headers=headers) as remote:

            def call(name: str, arguments: dict[str, object]) -> Any:
                return remote.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                ).json()["result"]

            result = call("memory_search", {"project_id": str(project), "query": f"{term}"})[
                "structuredContent"
            ]
            assert {item["text"] for item in result["items"]} == {
                f"{term} authoritative approved fact",
                f"{term} company visibility",
            }
            assert "messages" not in json.dumps(result) and "instructions" not in json.dumps(result)
            assert call("memory_search", {"project_id": str(other)})["isError"] is True
            data = capture(f"{term} proposed by external client", replaces_memory_id=current["id"])
            response = call("memory_capture", {"project_id": str(project), **data.model_dump(mode="json")})
            proposal = response["structuredContent"]["capture"]
            assert proposal["candidate"]["actor_id"] == grant.actor_id
            assert proposal["candidate"]["status"] == "pending"
            assert gateway.retrieve_memory(project, "authoritative", 20)[0]["id"] == current["id"]
            assert call("memory_review", {"decision": "approved"})["isError"] is True
            gateway.review_candidate(
                project, UUID(proposal["candidate"]["id"]), CandidateReview(decision="approved")
            )
            assert (
                call("memory_read", {"project_id": str(project), "memory_id": str(current["id"])})["content"][
                    0
                ]["text"]
                == "not_found"
            )
        gateway.engine.dispose()


@pytest.mark.parametrize("max_items", [1, 20])
def test_team_turn_retrieves_current_scoped_facts_and_retains_exact_snapshot(
    tmp_path: Path, max_items: int
) -> None:
    settings = _settings(tmp_path).model_copy(update={"role_context_max_items": max_items})
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, order_id = _order(client, "Copper team memory")
        project = UUID(project_id)
        gateway = owner()
        for index in range(max_items):
            publish(gateway, project, capture(f"Copper older project configured limit fact {index}"))
        old = publish(gateway, project, capture("Copper project fact"))
        company = publish(gateway, project, capture("Copper shared company fact", "company"))
        path = f"/api/projects/{project}/work-orders/{order_id}/team-session"
        session = client.post(path, headers=ORIGIN).json()["session"]
        bootstrap = create_engine(
            settings.database_url.set(username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"])
        )
        with bootstrap.begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": project, "s": UUID(session["id"])},
            )
            connection.execute(
                text("UPDATE profiles SET memory_scope='project' WHERE project_id=:p AND id=:id"),
                {"p": project, "id": UUID(profile_id)},
            )
        assert (
            client.post(
                f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        attempt = TaskSessionAttempts(gateway).claim_next(project)
        assert attempt is not None
        context = TaskSessionContext(gateway, settings)
        first = context.prepare(project, attempt["id"])
        assert "Copper project fact" in first.prompt and "Copper shared company fact" not in first.prompt
        assert (
            len(first.memory) == max_items
            and "Copper older project configured limit fact 0" not in first.prompt
        )
        assert first.memory[0]["id"] == str(old["id"]) and (
            settings.onefoundry_max_context_bytes is None
            or len(first.prompt.encode()) <= settings.onefoundry_max_context_bytes
        )
        publish(gateway, project, capture("Copper corrected project fact", replaces_memory_id=old["id"]))
        assert context.prepare(project, attempt["id"]) == first
        gateway.transition_memory(
            project, company["id"], MemoryTransition(action="withdraw", reason="Review")
        )
        with gateway.engine.connect() as connection:
            saved: Any = connection.execute(
                text("SELECT context_memory FROM task_session_attempts WHERE id=:id"), {"id": attempt["id"]}
            ).scalar_one()
            assert saved[0]["source_candidate_id"] == str(old["source_candidate_id"])
        bootstrap.dispose()
        gateway.engine.dispose()
