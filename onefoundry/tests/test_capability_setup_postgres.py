"""Real approval, repository read and exactly-once task continuation."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.capability_setup_resume import step
from onefoundry.main import create_app
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_runs import bind
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from tests.test_role_repository_file_unit import _git
from tests.test_self_extension_postgres import payload
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for


@pytest.fixture
def setup_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, ...]]:
    async def quiet(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", quiet)
    settings = settings_for(tmp_path)
    settings.local_source_root = str(tmp_path)
    settings.runner_spool_root = str(tmp_path / "runner-spool")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "README.md").write_text("A local application.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(
        engine,
        settings.operator_actor_id,
        runner_spool_root=settings.runner_spool_root,
        runner_limits=settings.runner_limits,
        runner_receipt_wait_seconds=settings.runner_receipt_wait_seconds,
        live_text_settings=settings,
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        base = f"/api/projects/{project}"
        source = client.post(
            base + "/sources/local", json={"root_id": "workspace", "relative_path": "repo"}, headers=ORIGIN
        ).json()
        order = client.post(
            base + "/work-orders",
            json={
                "title": "Read the repository",
                "instructions": "Explain architecture with citations.",
                "profile_id": profiles["Chief of Staff"],
                "deliverable_kind": "operator_review",
            },
            headers=ORIGIN,
        ).json()
        started = client.post(
            base + f"/work-orders/{order['id']}/team-session/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": order["version"],
                "body": "Inspect the repository and retain reviewed company knowledge.",
            },
            headers=ORIGIN,
        )
        assert started.status_code == 202, started.text
        attempts = TaskSessionAttempts(gateway)
        claimed = attempts.claim_next(UUID(project))
        assert claimed is not None
        bind(gateway, UUID(project), claimed)
        tools = TaskSessionTools(gateway, UUID(project), claimed["session_id"], claimed["id"])
        request = {
            "grants": [
                {"profile_id": profiles["Chief of Staff"], "tool_ids": ["repository_file_v1"]},
                {"profile_id": profiles["Engineer"], "tool_ids": ["repository_file_v1"]},
            ],
            "source_id": source["id"],
            "revision": source["revision"],
            "verification_path": "README.md",
            "reason": "Read the selected repository for the architecture task.",
            "checkpoint": "Explain the architecture from committed files and prepare reviewed knowledge.",
        }
        yield client, gateway, project, profiles, tools, claimed, request, repo
    engine.dispose()


def test_setup_is_approved_verified_and_resumed_once(setup_task: tuple[Any, ...]) -> None:
    client, gateway, project, profiles, tools, claimed, request, _repo = setup_task
    base = f"/api/projects/{project}"
    proposed = payload(tools.invoke("onefoundry.propose_capability_setup", request, "setup"))
    assert payload(tools.invoke("onefoundry.propose_capability_setup", request, "setup")) == proposed
    assert payload(tools.invoke("onefoundry.propose_capability_setup", request, "same-intent")) == proposed
    assert all("repository_file_v1" not in p["allowed_tools"] for p in client.get(base + "/profiles").json())
    assert not step(gateway)
    TaskSessionAttempts(gateway).settle(
        UUID(project), claimed["id"], "done", public_reply="Access requested."
    )
    approved = client.post(
        base + f"/approvals/{proposed['approval_id']}/decision", json={"decision": "approved"}, headers=ORIGIN
    )
    assert approved.status_code == 200, approved.text
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: step(gateway), range(2)))
    assert sum(outcomes) == 1
    assert not step(gateway)
    with gateway.engine.connect() as connection:
        observed = connection.scalar(
            text("SELECT payload FROM events WHERE project_id=:p AND event_type='capability_setup_resumed'"),
            {"p": UUID(project)},
        )
        assert observed["observation"]["status"] == "verified"
        assert len(observed["observation"]["checks"]) == 2
        assert (
            connection.scalar(
                text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p AND status='queued'"),
                {"p": UUID(project)},
            )
            == 1
        )
    agents = {p["id"]: p for p in client.get(base + "/profiles").json()}
    assert "repository_file_v1" in agents[profiles["Engineer"]]["allowed_tools"]
    assert "repository_file_v1" not in agents[profiles["Sales"]]["allowed_tools"]


@pytest.mark.parametrize("decision", ["rejected", "source_changed", "profile_changed", "unavailable"])
def test_setup_rejection_and_stale_owner_state(setup_task: tuple[Any, ...], decision: str) -> None:
    client, gateway, project, profiles, tools, claimed, request, repo = setup_task
    base = f"/api/projects/{project}"
    proposed = payload(tools.invoke("onefoundry.propose_capability_setup", request, "setup"))
    TaskSessionAttempts(gateway).settle(
        UUID(project), claimed["id"], "done", public_reply="Access requested."
    )
    if decision == "source_changed":
        assert client.delete(base + f"/sources/{request['source_id']}", headers=ORIGIN).status_code == 204
    if decision == "profile_changed":
        assert (
            client.patch(
                base + f"/profiles/{profiles['Engineer']}",
                json={"allowed_tools": ["project_brief_v1"]},
                headers=ORIGIN,
            ).status_code
            == 200
        )
    if decision == "unavailable":
        (repo / "README.md").write_text("Changed revision.\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "changed")
    response = client.post(
        base + f"/approvals/{proposed['approval_id']}/decision",
        json={"decision": "rejected" if decision == "rejected" else "approved"},
        headers=ORIGIN,
    )
    if decision in {"source_changed", "profile_changed"}:
        assert response.status_code in {404, 409}, response.text
        agents = client.get(base + "/profiles").json()
        assert all("repository_file_v1" not in p["allowed_tools"] for p in agents)
        assert not step(gateway)
    else:
        assert response.status_code == 200, response.text
        assert step(gateway)
        with gateway.engine.connect() as connection:
            result = connection.scalar(
                text(
                    "SELECT payload FROM events WHERE project_id=:p AND event_type='capability_setup_resumed'"
                ),
                {"p": UUID(project)},
            )
        assert result["observation"]["status"] == ("rejected" if decision == "rejected" else "blocked")


@pytest.mark.parametrize(
    "invalid", ["foreign_profile", "foreign_source", "unknown_tool", "secret_path", "missing_verification"]
)
def test_invalid_setup_has_no_approval(setup_task: tuple[Any, ...], invalid: str) -> None:
    client, _gateway, project, _profiles, tools, _claimed, request, _repo = setup_task
    if invalid == "foreign_profile":
        request["grants"][0]["profile_id"] = str(uuid4())
    elif invalid == "foreign_source":
        request["source_id"] = str(uuid4())
    elif invalid == "unknown_tool":
        request["grants"][0]["tool_ids"] = ["not_implemented_v1"]
    elif invalid == "secret_path":
        request["verification_path"] = ".env"
    else:
        request["verification_path"] = None
    assert not tools.invoke("onefoundry.propose_capability_setup", request, "invalid")["success"]
    assert client.get(f"/api/projects/{project}/approvals").json() == []
