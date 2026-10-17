"""Chief reads complete nested team state through the real SDK and scoped database."""

import json
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import chief_session_tools
from onefoundry.chief_workflow_summary import workflow_summary
from onefoundry.gateway_errors import Conflict
from onefoundry.main import create_app
from onefoundry.profile_text_runtime import ProfileTextRuntime
from onefoundry.settings import Settings
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_runs import bind
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.task_session_tools import READ_ONLY, TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from tests.test_team_workflow_postgres import ORIGIN, _tool_reply, project_with_models, settings_for


def _step(name: str, arguments: dict[str, Any]) -> ModelStep:
    return ModelStep(output=[function_call(name, arguments, call_id=str(uuid4()))])


def _payload(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["success"], reply
    return dict(json.loads(reply["contentItems"][0]["text"]))


def _documents(tools: TaskSessionTools, name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    documents = []
    offset = 0
    cutoff = None
    while True:
        pieces = []
        content_offset = 0
        digest = None
        while True:
            values = {**arguments, "offset": offset, "content_offset": content_offset}
            if cutoff:
                values["created_before"] = cutoff
            if digest:
                values["expected_sha256"] = digest
            key = f"{name}-{arguments.get('section', '')}-{arguments.get('session_id', '')}"
            key += f"-{offset}-{content_offset}"
            response = tools.invoke(name, values, key)
            assert tools.invoke(name, values, key) == response
            page = _payload(response)
            cutoff = page["created_before"]
            pieces.append(page["content"])
            if page["next_content_offset"] is None:
                break
            content_offset = page["next_content_offset"]
            digest = page["body_sha256"]
        documents.append(json.loads("".join(pieces)))
        assert len(documents) < 256, "inspection must not chase its own receipt stream"
        if page["next_offset"] is None:
            return documents
        offset = page["next_offset"]


@pytest.mark.parametrize("custom_chief", [False, True])
def test_sdk_chief_observes_nested_sessions_and_project_orders_without_accepting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_chief: bool,
) -> None:
    async def quiet(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", quiet)
    settings = settings_for(tmp_path).model_copy(
        update={"role_context_max_items": 1, "live_text_max_prompt_bytes": 4096}
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        pid = UUID(project)
        if custom_chief:
            admin = create_engine(Settings().database_url)
            with admin.begin() as connection:
                connection.execute(
                    text("UPDATE profiles SET kind='custom' WHERE project_id=:p AND id=:id"),
                    {"p": pid, "id": UUID(profiles["Chief of Staff"])},
                )
            admin.dispose()
        grant = client.patch(
            f"/api/projects/{project}/profiles/{profiles['Researcher']}",
            json={"allowed_tools": ["delegate_agent_v1"]},
            headers=ORIGIN,
        )
        assert grant.status_code == 200, grant.text

        def make_order(title: str) -> dict[str, Any]:
            response = client.post(
                f"/api/projects/{project}/work-orders",
                json={
                    "title": title,
                    "instructions": "Use saved workflow evidence.",
                    "profile_id": profiles["Chief of Staff"],
                    "deliverable_kind": "operator_review",
                },
                headers=ORIGIN,
            )
            assert response.status_code == 201, response.text
            return dict(response.json())

        order = make_order("Chief nested visibility")
        sibling = make_order("Other draft in the same project")
        base = f"/api/projects/{project}/work-orders/{order['id']}/team-session"
        started = client.post(
            f"{base}/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": order["version"],
                "body": "Start work.",
            },
            headers=ORIGIN,
        )
        assert started.status_code == 202, started.text
        ids: dict[str, str] = {}

        def save_assignment(call: Any) -> list[Any]:
            reply = _tool_reply(call)
            ids["research"] = reply["child_session_id"]
            return [assistant_message("Research assignment saved.")]

        def save_nested(call: Any) -> list[Any]:
            ids["nested"] = _tool_reply(call)["child_session_id"]
            return [function_call("publish_update", {"body": ""}, call_id="malformed")]

        progress = ("Public progress café 中文. " * 100).strip()
        models = [
            ScriptedModel(
                [
                    _step("read_project_status", {}),
                    _step(
                        "delegate_agent",
                        {
                            "profile_id": profiles["Researcher"],
                            "objective": "Collect findings",
                            "expected_result": "Saved finding",
                            "acceptance_check": "Use evidence",
                        },
                    ),
                    ModelStep.respond(save_assignment),
                ]
            ),
            ScriptedModel(
                [
                    _step(
                        "delegate_agent",
                        {
                            "profile_id": profiles["GTM"],
                            "objective": "Nested source review",
                            "expected_result": "Saved nested finding",
                            "acceptance_check": "Use evidence",
                        },
                    ),
                    ModelStep.respond(save_nested),
                    _step("publish_update", {"body": progress}),
                    _step(
                        "report_result",
                        {"body": "Research finding saved; nested work is separately tracked."},
                    ),
                    ModelStep(output=[assistant_message("Research completed.")]),
                ]
            ),
            ScriptedModel(
                [
                    _step(
                        "report_result", {"body": "Nested finding, with unresolved implementation questions."}
                    ),
                    ModelStep(output=[assistant_message("Nested report saved.")]),
                ]
            ),
        ]

        def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
            return TeamModel(models.pop(0), runtime.provider, runtime.model_id, False, False)

        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        worker = TaskSessionSdkTurn(gateway, settings, model_factory=factory)
        for _ in range(3):
            result = worker.run_next(pid)
            assert result is not None and result["outcome"] == "done", result
        before = client.get(base).json()
        assert len(before["assignments"]) == 2
        assert before["assignments"][1]["parent_session_id"] == ids["research"]
        with gateway.engine.connect() as connection:
            summary = workflow_summary(connection, pid, UUID(order["id"]))
        assert summary["assignment_counts"] == {"satisfied": 2}
        assert summary["attempt_counts"] == {"done": 3}
        with pytest.raises(Conflict, match="canonical"):
            chief_session_tools.execute(
                gateway, pid, UUID(ids["research"]), chief_session_tools.READ_PROJECT, {}
            )
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
        active = TaskSessionAttempts(gateway).claim_next(pid)
        assert active is not None
        bind(gateway, pid, active)
        tools = TaskSessionTools(gateway, pid, active["session_id"], active["id"])
        with gateway.engine.begin() as connection:
            for name in READ_ONLY:
                connection.execute(
                    text(
                        "INSERT INTO task_session_tool_calls (id,project_id,actor_id,root_work_order_id,"
                        "session_id,attempt_id,provider_call_id,tool_name,arguments_sha256) "
                        "VALUES (:id,:p,:actor,:root,:s,:attempt,:call,:name,:digest)"
                    ),
                    {
                        "id": uuid4(),
                        "p": pid,
                        "actor": "profile:" + profiles["Chief of Staff"],
                        "root": UUID(order["id"]),
                        "s": active["session_id"],
                        "attempt": UUID(before["attempts"][0]["id"]),
                        "call": name + "-interrupted",
                        "name": name,
                        "digest": "0" * 64,
                    },
                )
        assert tools.reconcile_previous_actions() == len(READ_ONLY)
        with gateway.engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM task_session_tool_calls WHERE project_id=:p "
                    "AND session_id=:s AND status='failed' AND reason='read_interrupted' AND result IS NULL"
                ),
                {"p": pid, "s": active["session_id"]},
            ) == len(READ_ONLY)
        documents = _documents(tools, "onefoundry.read_project_status", {})
        assert all(page["capabilities"]["readiness"]["project_id"] == project for page in documents)
        assert all(
            page["capabilities"]["readiness"]["profile_id"] == profiles["Chief of Staff"]
            for page in documents
        )
        assert any(
            check["operation"] == "project_code_change"
            for check in documents[0]["capabilities"]["readiness"]["checks"]
        )
        assert {str(order["id"]), str(sibling["id"])} <= {
            item["id"] for page in documents for item in page["work_orders"]
        }
        for section in (
            "overview",
            "sessions",
            "assignments",
            "dependencies",
            "decisions",
            "runs",
            "artifacts",
            "approvals",
            "events",
        ):
            documents = _documents(
                tools, "onefoundry.read_work_order", {"work_order_id": order["id"], "section": section}
            )
            if section == "sessions":
                assert ids["nested"] in {item["id"] for page in documents for item in page["records"]}
        messages = _documents(
            tools, "onefoundry.read_agent_session", {"session_id": ids["research"], "section": "messages"}
        )
        assert progress in [item["body"] for page in messages for item in page["records"]]
        actions = _documents(
            tools, "onefoundry.read_agent_session", {"session_id": ids["research"], "section": "actions"}
        )
        assert any(
            item["status"] == "failed" and item["reason"] == "invalid_tool_input"
            for page in actions
            for item in page["records"]
        )
        assert (
            _documents(
                tools, "onefoundry.read_agent_session", {"session_id": ids["nested"], "section": "attempts"}
            )[0]["records"][0]["status"]
            == "done"
        )
        # Status changes during Chief's turn revoke mutation tools, never read-only oversight.
        with gateway.engine.begin() as connection:
            connection.execute(
                text("UPDATE work_orders SET status='needs_review' WHERE project_id=:p AND id=:id"),
                {"p": pid, "id": UUID(order["id"])},
            )
        assert _payload(
            tools.invoke("onefoundry.read_work_order", {"work_order_id": sibling["id"]}, "other-order")
        )
        assert _payload(
            tools.invoke(
                "onefoundry.read_agent_session", {"session_id": ids["nested"]}, "after-status-change"
            )
        )
        with pytest.raises(Conflict, match="not admitted"):
            tools.invoke("onefoundry.delegate_agent", {}, "no-action-authority")
        foreign_project, foreign_profiles = project_with_models(client)
        foreign_order = client.post(
            f"/api/projects/{foreign_project}/work-orders",
            json={
                "title": "Different project record",
                "instructions": "Project isolation fixture",
                "profile_id": foreign_profiles["Chief of Staff"],
                "deliverable_kind": "operator_review",
            },
            headers=ORIGIN,
        ).json()
        foreign_start = client.post(
            f"/api/projects/{foreign_project}/work-orders/{foreign_order['id']}/team-session/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": foreign_order["version"],
                "body": "Foreign project isolation fixture.",
            },
            headers=ORIGIN,
        )
        assert foreign_start.status_code == 202, foreign_start.text
        foreign_session = foreign_start.json()["session_id"]
        for name, values in (
            ("onefoundry.read_work_order", {"work_order_id": foreign_order["id"]}),
            ("onefoundry.read_agent_session", {"session_id": foreign_session}),
        ):
            assert not tools.invoke(name, values, name + "-foreign")["success"]
        for name, arguments in (
            ("onefoundry.read_work_order", {"work_order_id": str(uuid4())}),
            ("onefoundry.read_agent_session", {"session_id": str(uuid4())}),
        ):
            assert not tools.invoke(name, arguments, name + "-outside")["success"]
        after = client.get(base).json()
        assert after["assignments"] == before["assignments"]
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM work_orders WHERE project_id=:p AND id=:id"),
                    {"p": pid, "id": UUID(sibling["id"])},
                )
                == "draft"
            )
        TaskSessionAttempts(gateway).settle(pid, active["id"], "done", public_reply="Inspection saved.")
        migration = import_module("migrations.versions.0078_chief_inspection_reads")
        with gateway.engine.connect() as connection, monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(migration.op, "get_bind", lambda: connection)
            with pytest.raises(RuntimeError, match="checkpoint"):
                migration.downgrade()
        gateway.engine.dispose()
