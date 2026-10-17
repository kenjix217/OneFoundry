"""Approved team source access reaches the bounded owner through real SDK turns."""

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.profile_text_runtime import ProfileTextRuntime
from onefoundry.settings import Settings
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.tool_gateway import ToolGateway
from tests.test_role_repository_file_unit import _git
from tests.test_team_workflow_postgres import ORIGIN, _step, _tool_reply, project_with_models, settings_for


def test_approved_engineer_and_qa_read_committed_source_in_native_team_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def quiet(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", quiet)
    root = tmp_path / "source-root"
    repo = root / "fixture"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "src/answer.py").write_text("answer = 42\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "saved fixture")
    settings = Settings(
        **{
            **settings_for(tmp_path).model_dump(),
            "local_source_root": str(root),
            "team_repository_read_enabled": True,
        }
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        base = f"/api/projects/{project}"
        chief = next(p for p in client.get(base + "/profiles").json() if p["name"] == "Chief of Staff")
        for name in ("Engineer", "QA"):
            changed = client.patch(
                base + f"/profiles/{profiles[name]}",
                json={
                    "model": chief["model"],
                    "model_account_id": chief["model_account_id"],
                    "instructions": "Read the approved committed fixture source.",
                    "allowed_tools": ["repository_file_v1"],
                    "autonomy_level": "supervised",
                },
                headers=ORIGIN,
            )
            assert changed.status_code == 200, changed.text
        source = client.post(
            base + "/sources/local", json={"root_id": "workspace", "relative_path": "fixture"}, headers=ORIGIN
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]
        revision = source.json()["revision"]
        # The current working file cannot replace the committed evidence.
        (repo / "src/answer.py").write_text("answer = 999\n")
        order = client.post(
            base + "/work-orders",
            json={
                "title": "Native source access regression",
                "instructions": "Review committed source independently.",
                "profile_id": profiles["Chief of Staff"],
                "deliverable_kind": "operator_review",
            },
            headers=ORIGIN,
        ).json()
        session = base + f"/work-orders/{order['id']}/team-session"
        started = client.post(
            session + "/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": order["version"],
                "body": "Engineer and QA must each read committed source.",
            },
            headers=ORIGIN,
        )
        assert started.status_code == 202, started.text
        ids: dict[str, str] = {}
        reads: list[str] = []

        def delegate_qa(call: Any) -> list[Any]:
            ids["engineer"] = _tool_reply(call)["assignment_id"]
            return [
                function_call(
                    "delegate_agent",
                    {
                        "profile_id": profiles["QA"],
                        "objective": "Read source and independently review Engineer.",
                        "expected_result": "Independent source verdict",
                        "acceptance_check": "Exact committed bytes",
                        "acceptance_kind": "qa_review",
                        "depends_on": [ids["engineer"]],
                    },
                    call_id="qa",
                )
            ]

        def source_verified(call: Any) -> list[Any]:
            receipt = _tool_reply(call)
            assert receipt["status"] == "done", receipt
            result = receipt["result"]
            assert result["text"] == "answer = 42\n" and result["revision"] == revision
            assert result["sha256"] == hashlib.sha256(b"answer = 42\n").hexdigest()
            assert result["truncated"] is False
            reads.append(receipt["result_sha256"])
            return [
                function_call(
                    "report_result",
                    {
                        "body": "Exact committed source read and verified.",
                        **({"verdict": "passed"} if len(reads) == 2 else {}),
                    },
                    call_id="report",
                )
            ]

        request = {
            "tool_id": "repository_file_v1",
            "arguments": {"source_id": source_id, "path": "src/answer.py"},
        }
        models = [
            ScriptedModel(
                [
                    _step(
                        "delegate_agent",
                        {
                            "profile_id": profiles["Engineer"],
                            "objective": "Read src/answer.py in the registered fixture.",
                            "expected_result": "Committed source finding",
                            "acceptance_check": "Read the owner receipt",
                        },
                    ),
                    ModelStep.respond(delegate_qa),
                    ModelStep(output=[assistant_message("Yielding to specialists.")]),
                ]
            ),
            ScriptedModel(
                [
                    _step("role_tool", request),
                    ModelStep.respond(source_verified),
                    ModelStep(output=[assistant_message("Engineer finding saved.")]),
                ]
            ),
            ScriptedModel(
                [
                    _step("role_tool", request),
                    ModelStep.respond(source_verified),
                    ModelStep(output=[assistant_message("Source QA saved.")]),
                ]
            ),
        ]

        def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
            return TeamModel(models.pop(0), runtime.provider, runtime.model_id, False, False)

        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        worker = TaskSessionSdkTurn(gateway, settings, model_factory=factory)
        for _ in range(3):
            outcome = worker.run_next(UUID(project))
            assert outcome is not None and outcome["outcome"] == "done", outcome
        with gateway.engine.connect() as connection:
            states: list[Any] = list(
                connection.execute(
                    text(
                        "SELECT status FROM task_session_assignments WHERE project_id=:p "
                        "ORDER BY created_at,id"
                    ),
                    {"p": UUID(project)},
                )
                .scalars()
                .all()
            )
        assert states == ["satisfied", "satisfied"] and len(reads) == 2
        gateway.engine.dispose()
