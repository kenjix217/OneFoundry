"""Waiting and rejected QA reads retain scoped receipts through real SDK turns."""

import hashlib
import json
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
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_runs import bind
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from tests.test_team_workflow_postgres import ORIGIN, _step, _tool_reply, project_with_models, settings_for


def test_waiting_read_allows_specialists_to_run_and_rejected_qa_remains_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def quiet(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", quiet)
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        pid = UUID(project)
        order = client.post(
            f"/api/projects/{project}/work-orders",
            json={
                "title": "Dependency availability regression",
                "instructions": "Collect a finding, then review it independently.",
                "profile_id": profiles["Chief of Staff"],
                "deliverable_kind": "operator_review",
            },
            headers=ORIGIN,
        ).json()
        base = f"/api/projects/{project}/work-orders/{order['id']}/team-session"
        started = client.post(
            f"{base}/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": order["version"],
                "body": "Start the declared review workflow.",
            },
            headers=ORIGIN,
        )
        assert started.status_code == 202, started.text
        ids: dict[str, str] = {}

        def read_queued(call: Any) -> list[Any]:
            ids["research"] = _tool_reply(call)["assignment_id"]
            return [
                function_call("read_dependency_result", {"assignment_id": ids["research"]}, call_id="wait")
            ]

        def delegate_qa(call: Any) -> list[Any]:
            receipt = _tool_reply(call)
            assert receipt["status"] == "pending" and receipt["content_available"] is False
            assert "Finish this turn" in receipt["next_step"]
            return [
                function_call(
                    "delegate_agent",
                    {
                        "profile_id": profiles["QA"],
                        "objective": "Review the declared finding",
                        "expected_result": "Independent verdict",
                        "acceptance_check": "Do not accept an unsupported finding",
                        "acceptance_kind": "qa_review",
                        "depends_on": [ids["research"]],
                    },
                    call_id="qa",
                )
            ]

        def finish_chief(call: Any) -> list[Any]:
            ids["qa"] = _tool_reply(call)["assignment_id"]
            return [assistant_message("Assignments are saved; waiting for their real results.")]

        chief = ScriptedModel(
            [
                _step(
                    "delegate_agent",
                    {
                        "profile_id": profiles["Researcher"],
                        "objective": "Collect the supplied finding",
                        "expected_result": "Source-backed finding",
                        "acceptance_check": "Distinguish verified facts from unknowns",
                    },
                ),
                ModelStep.respond(read_queued),
                ModelStep.respond(delegate_qa),
                ModelStep.respond(finish_chief),
            ]
        )
        finding = "Source evidence remains incomplete; do not claim implementation success."
        feedback = ("Needs repository evidence before the design can be approved. " * 150).strip()
        models = [
            chief,
            ScriptedModel(
                [
                    _step("report_result", {"body": finding}),
                    ModelStep(output=[assistant_message("Finding saved.")]),
                ]
            ),
            ScriptedModel(
                [
                    _step("report_result", {"body": feedback, "verdict": "needs_rework"}),
                    ModelStep(output=[assistant_message("QA requires rework.")]),
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
        view = client.get(base).json()
        assert [a["status"] for a in view["assignments"]] == ["satisfied", "blocked"]
        waiting = next(t for t in view["tool_calls"] if t["tool_name"] == "onefoundry.read_dependency_result")
        assert waiting["status"] == "done" and waiting["reason"] is None
        assert waiting["result"]["status"] == "pending"
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
        active = TaskSessionAttempts(gateway).claim_next(pid)
        assert active is not None
        bind(gateway, pid, active)
        tools = TaskSessionTools(gateway, pid, active["session_id"], active["id"])
        completed = _payload(
            tools.invoke("onefoundry.read_dependency_result", {"assignment_id": ids["research"]}, "ready")
        )
        assert completed["content"] == finding
        assert completed["body_sha256"] == hashlib.sha256(finding.encode()).hexdigest()
        parts = []
        offset = 0
        while True:
            response = tools.invoke(
                "onefoundry.read_dependency_result",
                {"assignment_id": ids["qa"], "offset": offset},
                f"qa-{offset}",
            )
            page = _payload(response)
            assert page["status"] == "blocked" and page["result_kind"] == "review_feedback"
            assert page["content_available"] is True
            assert (
                tools.invoke(
                    "onefoundry.read_dependency_result",
                    {"assignment_id": ids["qa"], "offset": offset},
                    f"qa-{offset}",
                )
                == response
            )
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        body = "".join(parts)
        assert json.loads(body)["feedback"] == feedback
        assert page["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
        assert not tools.invoke(
            "onefoundry.read_dependency_result", {"assignment_id": str(uuid4())}, "unknown"
        )["success"]
        rejected = tools.invoke(
            "onefoundry.delegate_agent",
            {
                "profile_id": profiles["Researcher"],
                "objective": "Revise the finding using rejected QA as a prerequisite",
                "expected_result": "An evidence-backed revision",
                "acceptance_check": "Must not wait forever on rejected QA",
                "depends_on": [ids["qa"]],
            },
            "rejected-dependency",
        )
        assert not rejected["success"], "blocked QA cannot become a permanently pending prerequisite"
        assert json.loads(rejected["contentItems"][0]["text"])["reason"] == "dependency_not_accepted"
        rejected_arguments = {
            "profile_id": profiles["Researcher"],
            "objective": "Revise the finding using rejected QA as a prerequisite",
            "expected_result": "An evidence-backed revision",
            "acceptance_check": "Must not wait forever on rejected QA",
            "depends_on": [ids["qa"]],
        }
        assert (
            tools.invoke("onefoundry.delegate_agent", rejected_arguments, "rejected-dependency") == rejected
        )
        with gateway.engine.connect() as connection:
            old = connection.scalar(
                text("SELECT result FROM task_session_tool_calls WHERE project_id=:p AND id=:id"),
                {"p": pid, "id": UUID(waiting["id"])},
            )
            assert old == waiting["result"], "a later result must not rewrite the original waiting receipt"
        assert client.get(base).json()["assignments"][1]["status"] == "blocked"
        _payload(tools.invoke("onefoundry.cancel_assignment", {"assignment_id": ids["qa"]}, "retire-qa"))
        canceled = _payload(
            tools.invoke("onefoundry.read_dependency_result", {"assignment_id": ids["qa"]}, "canceled-qa")
        )
        assert canceled["status"] == "canceled" and canceled["content_available"] is False
        assert not tools.invoke("onefoundry.delegate_agent", rejected_arguments, "canceled-dependency")[
            "success"
        ]
        replacement = _payload(
            tools.invoke(
                "onefoundry.delegate_agent",
                {
                    "profile_id": profiles["Researcher"],
                    "objective": "Provide the missing supplied evidence per QA: " + feedback,
                    "expected_result": "A corrected source-backed finding",
                    "acceptance_check": "Read the declared original finding and address QA feedback",
                    "depends_on": [ids["research"]],
                    "plan_revision": 2,
                },
                "revision",
            )
        )
        revised_qa = _payload(
            tools.invoke(
                "onefoundry.delegate_agent",
                {
                    "profile_id": profiles["QA"],
                    "objective": "Review the revised finding against the original source",
                    "expected_result": "An independent verdict on the revision",
                    "acceptance_check": "Read both declared findings before the verdict",
                    "acceptance_kind": "qa_review",
                    "depends_on": [ids["research"], replacement["assignment_id"]],
                    "plan_revision": 2,
                },
                "revised-qa",
            )
        )
        assert replacement["status"] == "ready" and revised_qa["status"] == "pending"
        TaskSessionAttempts(gateway).settle(
            pid, active["id"], "done", public_reply="QA feedback read; evidence needed for rework."
        )
        models.extend(
            [
                ScriptedModel(
                    [
                        _step("report_result", {"body": "The corrected fixture evidence is now supplied."}),
                        ModelStep(output=[assistant_message("Revision saved.")]),
                    ]
                ),
                ScriptedModel(
                    [
                        _step(
                            "report_result",
                            {
                                "body": "Both findings reviewed; the revision addresses QA feedback.",
                                "verdict": "passed",
                            },
                        ),
                        ModelStep(output=[assistant_message("Revised finding passed QA.")]),
                    ]
                ),
            ]
        )
        for _ in range(2):
            result = worker.run_next(pid)
            assert result is not None and result["outcome"] == "done", result
        resumed = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert resumed.status_code == 202, resumed.text
        models.append(
            ScriptedModel(
                [
                    _step(
                        "submit_completion",
                        {"body": "Corrected findings and independent QA are saved for operator review."},
                    ),
                    ModelStep(output=[assistant_message("Ready for operator review.")]),
                ]
            )
        )
        result = worker.run_next(pid)
        assert result is not None and result["outcome"] == "done", result
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        assert next(row for row in orders if row["id"] == order["id"])["status"] == "needs_review"
        assert [a["status"] for a in client.get(base).json()["assignments"]] == [
            "satisfied",
            "canceled",
            "satisfied",
            "satisfied",
        ]
        gateway.engine.dispose()


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    assert response["success"], response
    return dict(json.loads(response["contentItems"][0]["text"]))
