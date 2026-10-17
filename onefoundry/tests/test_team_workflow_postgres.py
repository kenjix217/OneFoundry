"""A disposable realistic brief crosses SDK, PostgreSQL and artifact storage.

The scripted provider proves local handoffs, never live provider readiness.
"""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.profile_text_runtime import ProfileTextRuntime
from onefoundry.settings import Settings
from onefoundry.task_session_assignments import DelegateRequest, TaskSessionAssignments
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}
FACTS = "Source brief: the product runs locally. Revenue and launch date are unverified."
GOOD = "# Demo brief\nThe product runs locally [source brief]. Revenue and launch date are unverified."


@pytest.fixture(autouse=True)
def quiet_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", start)


def settings_for(tmp_path: Path, *, enabled: bool = True) -> Settings:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    (tmp_path / "artifacts").mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        artifact_workspace_root=str(tmp_path / "artifacts"),
        team_worker_enabled=enabled,
    )


def project_with_models(client: TestClient) -> tuple[str, dict[str, str]]:
    created = client.post("/api/projects", json={"name": "Disposable demo brief E2E"}, headers=ORIGIN)
    assert created.status_code == 201, created.text
    project = created.json()["id"]
    model_path = f"/api/projects/{project}/model"
    account = client.post(
        f"{model_path}/accounts",
        json={"provider": "ollama", "label": "Fixture local", "endpoint": "http://ollama:11434"},
        headers=ORIGIN,
    )
    assert account.status_code == 201, account.text
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("fixture-text", "Fixture", "text")],
    ):
        refreshed = client.post(f"{model_path}/accounts/{account.json()['id']}/refresh", headers=ORIGIN)
    assert refreshed.status_code == 200, refreshed.text
    profiles = {p["name"]: p["id"] for p in client.get(f"/api/projects/{project}/profiles").json()}
    for name in ("Chief of Staff", "Researcher", "GTM", "QA"):
        updated = client.patch(
            f"/api/projects/{project}/profiles/{profiles[name]}",
            json={
                "model": "fixture-text",
                "model_account_id": account.json()["id"],
                "allowed_tools": ["write_text_artifact_v1"] if name == "GTM" else [],
                "autonomy_level": "supervised",
                "instructions": f"Do the assigned {name} task using saved evidence.",
            },
            headers=ORIGIN,
        )
        assert updated.status_code == 200, updated.text
    return project, profiles


def _tool_reply(call: Any) -> dict[str, Any]:
    item = next(
        item
        for item in reversed(call.input)
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    outer = json.loads(item["output"])
    assert outer["success"] is True, outer
    return dict(outer)


def _step(name: str, arguments: dict[str, Any]) -> ModelStep:
    return ModelStep(output=[function_call(name, arguments, call_id="call_0")])


@pytest.mark.parametrize("chief_kind", ["builtin", "custom"])
def test_brief_dependency_artifact_qa_rework_and_operator_acceptance(tmp_path: Path, chief_kind: str) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        if chief_kind == "custom":
            # Historical projects retain a custom canonical Chief; real live
            # metadata demonstrates this shape and the hierarchy owns identity.
            bootstrap = create_engine(Settings().database_url)
            with bootstrap.begin() as connection:
                connection.execute(
                    text("UPDATE profiles SET kind='custom' WHERE project_id=:p AND id=:id"),
                    {"p": UUID(project), "id": UUID(profiles["Chief of Staff"])},
                )
            bootstrap.dispose()
        pid = UUID(project)
        order = client.post(
            f"/api/projects/{project}/work-orders",
            json={
                "title": "Produce a source-backed demo brief",
                "instructions": FACTS,
                "profile_id": profiles["Chief of Staff"],
                "deliverable_kind": "artifact",
                "acceptance_checks": ["Retrieve the brief; no unsupported claims; independent QA passed."],
            },
            headers=ORIGIN,
        )
        assert order.status_code == 201, order.text
        oid = order.json()["id"]
        base = f"/api/projects/{project}/work-orders/{oid}/team-session"
        assert client.get(f"{base}/readiness").json()["status"] == "draft"
        assert (
            client.post(
                f"{base}/activate", json={"expected_version": order.json()["version"]}, headers=ORIGIN
            ).status_code
            == 409
        )
        attempt_key = str(uuid4())
        start = {
            "client_message_id": attempt_key,
            "expected_version": order.json()["version"],
            "body": "Start this work and use the saved source brief.",
        }
        queued = client.post(f"{base}/start", json=start, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        replay = client.post(f"{base}/start", json=start, headers=ORIGIN)
        assert replay.status_code == 200 and replay.json() == queued.json()
        gateway = ToolGateway(
            create_engine(settings.database_url), "fixture-worker", live_text_settings=settings
        )
        ids: dict[str, str] = {}
        artifacts: dict[str, str] = {}

        def delegate(call: Any, role: str, depends: list[str], kind: str, revision: int = 1) -> list[Any]:
            prior = _tool_reply(call)
            ids["previous"] = prior["assignment_id"]
            return [
                function_call(
                    "delegate_agent",
                    {
                        "profile_id": profiles[role],
                        "objective": f"{role} brief revision {revision}",
                        "expected_result": "A verifiable source-backed result",
                        "acceptance_check": "Do not invent product claims",
                        "acceptance_kind": kind,
                        "depends_on": depends,
                        "plan_revision": revision,
                    },
                    call_id=f"delegate-{role}-{revision}",
                )
            ]

        def gtm_delegate(call: Any) -> list[Any]:
            ids["research"] = _tool_reply(call)["assignment_id"]
            return delegate(call, "GTM", [ids["research"]], "artifact")

        def qa_delegate(call: Any) -> list[Any]:
            ids["bad_gtm"] = _tool_reply(call)["assignment_id"]
            return delegate(call, "QA", [ids["bad_gtm"]], "qa_review")

        def remember_qa(call: Any) -> list[Any]:
            ids["bad_qa"] = _tool_reply(call)["assignment_id"]
            return [assistant_message("Research, production and QA are assigned with dependencies.")]

        chief = ScriptedModel(
            [
                _step(
                    "delegate_agent",
                    {
                        "profile_id": profiles["Researcher"],
                        "objective": "Verify the supplied product brief",
                        "expected_result": "Supported facts and unknowns",
                        "acceptance_check": "Only use supplied source brief",
                    },
                ),
                ModelStep.respond(gtm_delegate),
                ModelStep.respond(qa_delegate),
                ModelStep.respond(remember_qa),
            ]
        )
        research = ScriptedModel(
            [
                _step("report_result", {"body": FACTS}),
                ModelStep(output=[assistant_message("Verified brief saved.")]),
            ]
        )

        def artifact_model(content: str) -> ScriptedModel:
            def report_saved(call: Any) -> list[Any]:
                receipt = _tool_reply(call)
                artifacts.update({"id": receipt["result"]["artifact_id"], "run": receipt["run_id"]})
                return [
                    function_call(
                        "report_result",
                        {
                            "body": "Draft brief saved",
                            "artifact_id": artifacts["id"],
                            "artifact_run_id": artifacts["run"],
                        },
                        call_id="artifact-result",
                    )
                ]

            return ScriptedModel(
                [
                    _step(
                        "role_tool",
                        {
                            "tool_id": "write_text_artifact_v1",
                            "arguments": {"kind": "markdown", "content": content},
                        },
                    ),
                    ModelStep.respond(report_saved),
                    ModelStep(output=[assistant_message("Artifact saved for QA.")]),
                ]
            )

        bad_gtm = artifact_model("# Demo brief\nThe product makes millions in revenue.")
        bad_qa = ScriptedModel(
            [
                _step(
                    "report_result",
                    {
                        "body": "Reject: revenue is unsupported. Remove the claim and cite the brief.",
                        "verdict": "needs_rework",
                    },
                ),
                ModelStep(output=[assistant_message("QA failed; bounded rework required.")]),
            ]
        )
        models = [chief, research, bad_gtm, bad_qa]

        def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
            return TeamModel(models.pop(0), runtime.provider, runtime.model_id, False, False)

        worker = TaskSessionSdkTurn(gateway, settings, model_factory=factory)
        for _ in range(4):
            result = worker.run_next(pid)
            assert result is not None and result["outcome"] == "done", result
        assert FACTS in str(research.first_call)
        assert FACTS in str(bad_gtm.first_call)
        assert "millions in revenue" in str(bad_qa.first_call)
        view = client.get(base).json()
        assert [a["status"] for a in view["assignments"]] == ["satisfied", "satisfied", "blocked"]
        assert "revenue is unsupported" in view["messages"][-1]["body"]
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        assert len(orders) == 4
        assert next(w for w in orders if w["id"] == oid)["status"] == "running"
        for assignment in view["assignments"][1:]:
            session = next(s for s in view["children"] if s["id"] == assignment["child_session_id"])
            child_order = next(w for w in orders if w["id"] == session["work_order_id"])
            assert child_order["status"] == "failed"
            assert session["work_order_status"] == "failed"
        for assignment in view["assignments"][:1]:
            session = next(s for s in view["children"] if s["id"] == assignment["child_session_id"])
            child_order = next(w for w in orders if w["id"] == session["work_order_id"])
            assert child_order["status"] == "needs_review"
            resumed = client.post(
                f"{base}/sessions/{session['id']}/continue",
                json={"client_attempt_id": str(uuid4())},
                headers=ORIGIN,
            )
            assert resumed.status_code == 409

        def rework_gtm(call: Any) -> list[Any]:
            assert _tool_reply(call)["status"] == "canceled"
            return [
                function_call(
                    "delegate_agent",
                    {
                        "profile_id": profiles["GTM"],
                        "objective": "Correct the revenue claim per QA feedback",
                        "expected_result": "Corrected cited brief",
                        "acceptance_check": "No unsupported revenue claims",
                        "acceptance_kind": "artifact",
                        "depends_on": [ids["research"]],
                        "plan_revision": 2,
                    },
                    call_id="rework-gtm",
                )
            ]

        def rework_qa(call: Any) -> list[Any]:
            ids["good_gtm"] = _tool_reply(call)["assignment_id"]
            return delegate(call, "QA", [ids["good_gtm"]], "qa_review", 2)

        replan = ScriptedModel(
            [
                _step("cancel_assignment", {"assignment_id": ids["bad_qa"]}),
                ModelStep.respond(rework_gtm),
                ModelStep.respond(rework_qa),
                ModelStep(output=[assistant_message("Rework assigned.")]),
            ]
        )
        good_gtm = artifact_model(GOOD)
        good_qa = ScriptedModel(
            [
                _step(
                    "report_result",
                    {
                        "body": "Passed: every product claim is cited or explicitly unverified.",
                        "verdict": "passed",
                    },
                ),
                ModelStep(output=[assistant_message("Exact artifact passed QA.")]),
            ]
        )
        models.extend([replan, good_gtm, good_qa])
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
        for _ in range(3):
            result = worker.run_next(pid)
            assert result is not None and result["outcome"] == "done", result
        assert "revenue is unsupported" in str(replan.first_call)
        assert FACTS in str(good_gtm.first_call)
        assert good_qa.first_call is not None
        original_input = good_qa.first_call.input
        texts = (
            [original_input]
            if isinstance(original_input, str)
            else [
                item["content"]
                for item in original_input
                if isinstance(item, dict) and isinstance(item.get("content"), str)
            ]
        )
        dependency_line = next(
            line
            for body in texts
            for line in body.splitlines()
            if line.startswith("Confirmed result from assignment")
        )
        assert json.loads(dependency_line.split(": ", 1)[1])["artifact"]["content"] == GOOD
        summary = ScriptedModel(
            [
                _step(
                    "submit_completion",
                    {
                        "body": "Corrected brief delivered, exact bytes reviewed by QA.",
                        "artifact_id": artifacts["id"],
                        "artifact_run_id": artifacts["run"],
                    },
                ),
                ModelStep(output=[assistant_message("Brief is ready for operator acceptance.")]),
            ]
        )
        models.append(summary)
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
        final = worker.run_next(pid)
        assert final is not None and final["outcome"] == "done", final
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        completed = next(w for w in orders if w["id"] == oid)
        assert completed["status"] == "needs_review", completed
        preview = client.get(
            f"/api/projects/{project}/runs/{artifacts['run']}/workspace-artifacts/{artifacts['id']}"
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["content"] == GOOD
        assert preview.json()["sha256"] == hashlib.sha256(GOOD.encode()).hexdigest()
        returned = client.post(
            f"/api/projects/{project}/work-orders/{oid}/return",
            json={
                "expected_version": completed["version"],
                "reason": "Explain that the unknown launch date is intentional.",
            },
            headers=ORIGIN,
        )
        assert returned.status_code == 200 and returned.json()["status"] == "ready", returned.text
        assert "unknown launch date" in client.get(base).json()["messages"][-1]["body"]
        stale_accept = client.post(
            f"/api/projects/{project}/work-orders/{oid}/accept",
            json={"expected_version": completed["version"], "reason": "Stale acceptance must not succeed."},
            headers=ORIGIN,
        )
        assert stale_accept.status_code == 409
        clarified = ScriptedModel(
            [
                _step(
                    "submit_completion",
                    {
                        "body": "Launch date remains unknown because the source does not establish it.",
                        "artifact_id": artifacts["id"],
                        "artifact_run_id": artifacts["run"],
                    },
                ),
                ModelStep(output=[assistant_message("Clarification saved for operator review.")]),
            ]
        )
        models.append(clarified)
        resumed = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert resumed.status_code == 202, resumed.text
        final = worker.run_next(pid)
        assert final is not None and final["outcome"] == "done", final
        assert "unknown launch date" in str(clarified.first_call)
        completed = next(
            w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid
        )
        assert completed["status"] == "needs_review"
        accepted = client.post(
            f"/api/projects/{project}/work-orders/{oid}/accept",
            json={
                "expected_version": completed["version"],
                "reason": "Read the artifact and verified the exact QA result.",
            },
            headers=ORIGIN,
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "done", accepted.text
        final_orders = client.get(f"/api/projects/{project}/work-orders").json()
        assert all(w["status"] in ("done", "failed") for w in final_orders)
        assert models == []
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM task_session_tool_calls "
                        "WHERE project_id=:p AND status='started'"
                    ),
                    {"p": pid},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM runs WHERE project_id=:p AND status='running'"), {"p": pid}
                )
                == 0
            )
        events = client.get(f"/api/projects/{project}/work-orders/{oid}/activity").json()
        assert any(event["event_type"] == "task_session_completion_submitted" for event in events)
        assert any(event["event_type"] == "task_session_branch_canceled" for event in events)
        gateway.engine.dispose()


def test_disabled_worker_refuses_new_turn_before_creating_queue(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, enabled=False)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        order = client.post(
            f"/api/projects/{project}/work-orders",
            json={
                "title": "Worker unavailable",
                "instructions": "No job should launch",
                "profile_id": profiles["Chief of Staff"],
            },
            headers=ORIGIN,
        ).json()
        path = f"/api/projects/{project}/work-orders/{order['id']}/team-session"
        assert client.post(path, headers=ORIGIN).status_code == 201
        continued = client.post(f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert continued.status_code == 409
        assert client.get(path).json()["attempts"] == []


def activate_fixture(client: TestClient) -> tuple[str, str, dict[str, str], str]:
    project, profiles = project_with_models(client)
    order = client.post(
        f"/api/projects/{project}/work-orders",
        json={
            "title": "Bounded fixture",
            "instructions": FACTS,
            "profile_id": profiles["Chief of Staff"],
            "deliverable_kind": "artifact",
        },
        headers=ORIGIN,
    ).json()
    oid = order["id"]
    ready = client.post(f"/api/projects/{project}/work-orders/{oid}/ready", headers=ORIGIN).json()
    base = f"/api/projects/{project}/work-orders/{oid}/team-session"
    response = client.post(f"{base}/activate", json={"expected_version": ready["version"]}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return project, oid, profiles, base


@pytest.mark.parametrize("interrupted", [False, True])
def test_researcher_corrects_rejected_verdict_inside_one_sdk_turn(tmp_path: Path, interrupted: bool) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, profiles, base = activate_fixture(client)
        queued = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        gateway = ToolGateway(create_engine(settings.database_url), "fixture", live_text_settings=settings)
        chief = ScriptedModel(
            [
                _step(
                    "delegate_agent",
                    {
                        "profile_id": profiles["Researcher"],
                        "objective": "Verify source facts",
                        "expected_result": "Source facts and unknowns",
                        "acceptance_check": "Only supplied source facts",
                    },
                ),
                ModelStep(output=[assistant_message("Research assigned.")]),
            ]
        )

        def correct_result(call: Any) -> list[Any]:
            item = next(
                item
                for item in reversed(call.input)
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            )
            rejected = json.loads(item["output"])
            assert rejected["success"] is False
            assert rejected["reason"] == "text_report_body_only"
            assert "body only" in rejected["next_step"]
            return [function_call("report_result", {"body": FACTS}, call_id="corrected-result")]

        researcher = ScriptedModel(
            [
                _step("report_result", {"body": FACTS, "verdict": "passed"}),
                ModelStep.respond(correct_result),
                ModelStep(output=[assistant_message("Corrected source result saved.")]),
            ]
        )
        models = [chief, researcher]

        def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
            return TeamModel(models.pop(0), runtime.provider, runtime.model_id, False, False)

        worker = TaskSessionSdkTurn(gateway, settings, model_factory=factory)
        if interrupted:
            chief_outcome = worker.run_next(UUID(project))
            assert chief_outcome is not None and chief_outcome["outcome"] == "done", chief_outcome
            attempts = TaskSessionAttempts(gateway)
            interrupted_attempt = attempts.claim_next(UUID(project))
            assert interrupted_attempt is not None
            tools = TaskSessionTools(
                gateway, UUID(project), interrupted_attempt["session_id"], interrupted_attempt["id"]
            )
            rejected_args = {"body": FACTS, "verdict": "passed"}
            failed = tools.invoke("onefoundry.report_result", rejected_args, "interrupted-result")
            assert failed["success"] is False
            assert tools.invoke("onefoundry.report_result", rejected_args, "interrupted-result") == failed
            attempts.settle(
                UUID(project), interrupted_attempt["id"], "unknown", reason="fixture_guard_interrupted"
            )
            child_path = f"{base}/sessions/{interrupted_attempt['session_id']}"
            assert (
                client.post(
                    f"{child_path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
                ).status_code
                == 409
            )
            with gateway.engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM task_session_tool_calls "
                            "WHERE project_id=:p AND status='started'"
                        ),
                        {"p": UUID(project)},
                    )
                    == 0
                )
            before = client.get(base).json()
            assert before["assignments"][0]["status"] == "blocked"
            assert before["assignments"][0]["result_message_id"] is None
            reconciled = client.post(f"{child_path}/reconcile", headers=ORIGIN)
            assert reconciled.status_code == 200, reconciled.text
            acknowledged = client.post(
                f"{child_path}/attempts/{interrupted_attempt['id']}/acknowledge",
                json={
                    "client_action_id": str(uuid4()),
                    "decision": "abandon_unverified",
                    "reason": (
                        "Failed result receipt, no committed result or outstanding action; "
                        "start a new bounded turn."
                    ),
                },
                headers=ORIGIN,
            )
            assert acknowledged.status_code == 200, acknowledged.text
            continued = client.post(
                f"{child_path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            )
            assert continued.status_code == 202, continued.text
            models[:] = [
                ScriptedModel(
                    [
                        _step("report_result", {"body": FACTS}),
                        ModelStep(output=[assistant_message("New verified source result saved.")]),
                    ]
                )
            ]
            outcome = worker.run_next(UUID(project))
            assert outcome is not None and outcome["outcome"] == "done", outcome
        else:
            for _ in range(2):
                outcome = worker.run_next(UUID(project))
                assert outcome is not None and outcome["outcome"] == "done", outcome
        view = client.get(base).json()
        child_view = client.get(f"{base}/sessions/{view['assignments'][0]['child_session_id']}").json()
        all_attempts = view["attempts"] + child_view["attempts"]
        assert len(all_attempts) == (3 if interrupted else 2)
        assert sum(attempt["status"] == "done" for attempt in all_attempts) == 2
        if interrupted:
            assert interrupted_attempt is not None
            preserved = next(
                attempt for attempt in all_attempts if attempt["id"] == str(interrupted_attempt["id"])
            )
            assert preserved["status"] == "unknown" and preserved["unknown_acknowledged"] is True
        assert view["assignments"][0]["status"] == "satisfied"
        assert view["assignments"][0]["result_message_id"] is not None
        assert any(
            message["id"] == view["assignments"][0]["result_message_id"] and message["body"] == FACTS
            for message in child_view["messages"]
        )
        with gateway.engine.connect() as connection:
            reports = (
                connection.execute(
                    text(
                        "SELECT status,reason FROM task_session_tool_calls "
                        "WHERE project_id=:p AND tool_name='onefoundry.report_result' ORDER BY created_at"
                    ),
                    {"p": UUID(project)},
                )
                .mappings()
                .all()
            )
        assert [dict(row) for row in reports] == [
            {"status": "failed", "reason": "text_report_body_only"},
            {"status": "done", "reason": None},
        ]
        assert models == []
        gateway.engine.dispose()


def test_explicit_legacy_cutover_preserves_history_and_queues_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        order = client.post(
            f"/api/projects/{project}/work-orders",
            json={
                "title": "Legacy cutover",
                "instructions": FACTS,
                "profile_id": profiles["Chief of Staff"],
            },
            headers=ORIGIN,
        ).json()
        base = f"/api/projects/{project}/work-orders/{order['id']}/team-session"
        saved = client.post(base, headers=ORIGIN).json()
        assert saved["session"]["execution_mode"] == "legacy"
        with create_engine(settings.database_url).begin() as c:
            c.execute(
                text(
                    "INSERT INTO task_session_messages (id,project_id,actor_id,root_work_order_id,session_id,"
                    "sequence,kind,body) VALUES (:id,:p,'operator',:root,:s,1,'system',"
                    "'Preserved legacy history.')"
                ),
                {
                    "id": uuid4(),
                    "p": UUID(project),
                    "root": UUID(order["id"]),
                    "s": UUID(saved["session"]["id"]),
                },
            )
        saved = client.get(base).json()
        ready = client.post(f"/api/projects/{project}/work-orders/{order['id']}/ready", headers=ORIGIN).json()
        assert (
            client.post(
                f"{base}/activate", json={"expected_version": ready["version"] + 1}, headers=ORIGIN
            ).status_code
            == 409
        )
        switched = client.post(
            f"{base}/activate", json={"expected_version": ready["version"]}, headers=ORIGIN
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["session"]["id"] == saved["session"]["id"]
        assert switched.json()["session"]["execution_mode"] == "team"
        assert switched.json()["messages"] == saved["messages"]
        assert switched.json()["attempts"] == []
        assert (
            client.post(
                f"{base}/activate", json={"expected_version": ready["version"]}, headers=ORIGIN
            ).status_code
            == 200
        )
        with create_engine(settings.database_url).connect() as c:
            assert (
                c.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p "
                        "AND event_type='task_session_team_enabled'"
                    ),
                    {"p": UUID(project)},
                )
                == 1
            )
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )


def test_unknown_acknowledgement_is_scoped_and_preserves_immutable_attempt(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, _profiles, base = activate_fixture(client)
        client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        gateway = ToolGateway(
            create_engine(settings.database_url), settings.operator_actor_id, live_text_settings=settings
        )
        attempts = TaskSessionAttempts(gateway)
        claimed = attempts.claim_next(UUID(project))
        assert claimed is not None
        attempts.settle(UUID(project), claimed["id"], "unknown", reason="fixture_interrupted")
        before = client.get(base).json()["attempts"][0]
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 409
        )
        path = f"{base}/sessions/{claimed['session_id']}/attempts/{claimed['id']}/acknowledge"
        data = {
            "client_action_id": str(uuid4()),
            "decision": "abandon_unverified",
            "reason": "Abandon uncertainty; do not replay any provider effect.",
        }
        assert (
            client.post(path, json={**data, "decision": "confirmed_success"}, headers=ORIGIN).status_code
            == 422
        )
        assert (
            client.post(
                path, json={**data, "reason": "postgresql://alice:password@db/secret"}, headers=ORIGIN
            ).status_code
            == 422
        )
        assert (
            client.post(path.replace(str(claimed["id"]), str(uuid4())), json=data, headers=ORIGIN).status_code
            == 404
        )
        assert client.post(path, json=data, headers=ORIGIN).status_code == 200
        assert client.post(path, json=data, headers=ORIGIN).status_code == 200
        assert (
            client.post(
                path,
                json={**data, "reason": "A different acknowledgement must not overwrite history."},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        after = client.get(base).json()["attempts"][0]
        assert after.pop("unknown_acknowledged") is True
        before.pop("unknown_acknowledged")
        assert after == before
        ready = next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)
        assert (
            client.post(
                f"{base}/cancel",
                json={"expected_version": ready["version"], "client_action_id": str(uuid4())},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        assert client.get(base).json()["attempts"][0]["status"] == "unknown"
        gateway.engine.dispose()


def test_cancel_queued_order_is_idempotent_and_scoped(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, _profiles, base = activate_fixture(client)
        attempt = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert attempt.status_code == 202
        order = next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)
        data = {"expected_version": order["version"], "client_action_id": str(uuid4())}
        assert client.post(f"{base}/cancel", json={**data, "extra": True}, headers=ORIGIN).status_code == 422
        wrong = base.replace(project, str(uuid4()))
        assert client.post(f"{wrong}/cancel", json=data, headers=ORIGIN).status_code == 404
        canceled = client.post(f"{base}/cancel", json=data, headers=ORIGIN)
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["queued_canceled"] == 1
        replay = client.post(f"{base}/cancel", json=data, headers=ORIGIN)
        assert replay.status_code == 200 and replay.json() == canceled.json()
        view = client.get(base).json()
        assert view["session"]["status"] == "closed"
        assert view["attempts"][-1]["status"] == "failed"
        assert view["attempts"][-1]["reason"] == "operator_canceled"
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 409
        )


@pytest.mark.parametrize("state", ["running", "unknown", "unconfirmed_action"])
def test_cancel_refuses_active_or_uncertain_attempt(tmp_path: Path, state: str) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, _profiles, base = activate_fixture(client)
        client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        gateway = ToolGateway(create_engine(settings.database_url), "fixture", live_text_settings=settings)
        attempts = TaskSessionAttempts(gateway)
        claimed = attempts.claim_next(UUID(project))
        assert claimed is not None
        if state == "unknown":
            attempts.settle(UUID(project), claimed["id"], "unknown", reason="fixture_interrupted")
        elif state == "unconfirmed_action":
            tools = TaskSessionTools(gateway, UUID(project), claimed["session_id"], claimed["id"])
            tools._begin(
                tools._scope(), "onefoundry.publish_update", "fixture-unconfirmed", "a" * 64, allow_new=True
            )
            attempts.settle(UUID(project), claimed["id"], "failed", reason="fixture_interrupted")
        order = next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)
        result = client.post(
            f"{base}/cancel",
            json={"expected_version": order["version"], "client_action_id": str(uuid4())},
            headers=ORIGIN,
        )
        assert result.status_code == 409
        assert client.get(base).json()["attempts"][-1]["status"] == (
            "failed" if state == "unconfirmed_action" else state
        )
        gateway.engine.dispose()


def test_manual_child_waits_and_text_report_cannot_finish_artifact_order(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, profiles, base = activate_fixture(client)
        assert (
            client.patch(
                f"/api/projects/{project}/profiles/{profiles['Researcher']}",
                json={"autonomy_level": "manual"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        gateway = ToolGateway(create_engine(settings.database_url), "fixture", live_text_settings=settings)
        attempts = TaskSessionAttempts(gateway)
        chief = attempts.claim_next(UUID(project))
        assert chief is not None
        assignment = TaskSessionAssignments(gateway).delegate(
            UUID(project),
            chief["session_id"],
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["Researcher"]),
                objective="Verify brief",
                expected_result="Source facts",
                acceptance_check="No invented claims",
            ),
        )
        child = client.get(f"{base}/sessions/{assignment['child_session_id']}").json()
        assert child["session"]["status"] == "waiting_for_operator" and child["attempts"] == []
        child = child["session"]
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        assert next(w for w in orders if w["id"] == child["work_order_id"])["status"] == "draft"
        assert (
            client.post(
                f"{base}/sessions/{child['id']}/continue",
                json={"client_attempt_id": str(uuid4())},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/projects/{project}/work-orders/{child['work_order_id']}/ready", headers=ORIGIN
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{base}/sessions/{child['id']}/continue",
                json={"client_attempt_id": str(uuid4())},
                headers=ORIGIN,
            ).status_code
            == 202
        )
        attempts.settle(UUID(project), chief["id"], "done", public_reply="Manual research assigned.")
        child_attempt = attempts.claim_next(UUID(project))
        assert child_attempt is not None
        result = TaskSessionTools(gateway, UUID(project), UUID(child["id"]), child_attempt["id"]).invoke(
            "onefoundry.report_result", {"body": "{a non-JSON source note"}, "fixture-report"
        )
        assert result["success"] is True
        attempts.settle(UUID(project), child_attempt["id"], "done", public_reply="Facts saved.")
        assert TaskSessionDecisions(gateway).dispatch_next(UUID(project)) is not None
        final = attempts.claim_next(UUID(project))
        assert final is not None
        rejected = TaskSessionTools(gateway, UUID(project), final["session_id"], final["id"]).invoke(
            "onefoundry.submit_completion", {"body": "Claiming the artifact is ready"}, "bad-completion"
        )
        assert rejected["success"] is False
        assert (
            next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)[
                "status"
            ]
            != "done"
        )
        gateway.engine.dispose()


def test_result_boundary_refuses_credential_uri_before_owner_access() -> None:
    from onefoundry.task_session_results import CompletionArgs, ReportArgs, report, submit_completion

    # No real credential: the reserved invalid host and literal placeholder
    # exercise the stronger shared role-text boundary, not only token patterns.
    rejected = "postgresql://fixture_user:fixture_placeholder@database.invalid/db"
    for owner, data in (
        (report, ReportArgs(body=rejected)),
        (submit_completion, CompletionArgs(body=rejected)),
    ):
        with pytest.raises(ValueError, match="sensitive result"):
            owner(None, {}, uuid4(), data)  # type: ignore[arg-type]


def _queue_continuation(client: TestClient, base: str) -> str:
    queued = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
    assert queued.status_code == 202, queued.text
    return str(queued.json()["attempt_id"])


def _scripted_worker(
    gateway: ToolGateway, settings: Settings, models: list[ScriptedModel]
) -> TaskSessionSdkTurn:
    def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
        return TeamModel(models.pop(0), runtime.provider, runtime.model_id, False, False)

    return TaskSessionSdkTurn(gateway, settings, model_factory=factory)


def _confirmed_turns(
    client: TestClient,
    base: str,
    project: UUID,
    worker: TaskSessionSdkTurn,
    models: list[ScriptedModel],
    start: int,
    stop: int,
) -> None:
    for number in range(start, stop):
        models.append(ScriptedModel([ModelStep(output=[assistant_message(f"Saved checkpoint {number}.")])]))
        _queue_continuation(client, base)
        result = worker.run_next(project)
        assert result is not None and result["outcome"] == "done", result
        # Empty polling is not autonomous progress and must not spend a model call.
        assert TaskSessionDecisions(worker.gateway).dispatch_next(project) is None
        assert worker.run_next(project) is None
        assert models == []


def test_team_goal_continues_past_100_turns_after_app_and_worker_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    assert settings.task_conversation_max_turns == 100
    gateway = ToolGateway(create_engine(settings.database_url), "fixture-worker", live_text_settings=settings)
    models: list[ScriptedModel] = []
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, profiles, base = activate_fixture(client)
        pid = UUID(project)
        _confirmed_turns(client, base, pid, _scripted_worker(gateway, settings, models), models, 0, 40)
        before = client.get(base).json()
        assert len(before["attempts"]) == 40
        resumed_id = _queue_continuation(client, base)
        session_id = before["session"]["id"]
    gateway.engine.dispose()

    # New app, gateway and worker instances resume the persisted queue, not an in-memory loop.
    gateway = ToolGateway(
        create_engine(settings.database_url), "fixture-restarted", live_text_settings=settings
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        reopened = client.get(base).json()
        assert reopened["session"]["id"] == session_id
        assert reopened["attempts"][-1]["id"] == resumed_id
        models.append(ScriptedModel([ModelStep(output=[assistant_message("Resumed checkpoint 40.")])]))
        worker = _scripted_worker(gateway, settings, models)
        assert (worker.run_next(pid) or {})["outcome"] == "done"
        _confirmed_turns(client, base, pid, worker, models, 41, 105)
        saved = client.get(base).json()
        assert len(saved["attempts"]) == 105
        assert all(a["status"] == "done" for a in saved["attempts"])
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        assert next(w for w in orders if w["id"] == oid)["status"] == "running"
        assert saved["session"]["status"] != "closed"

        ids: dict[str, str] = {}

        def delegate_qa(call: Any) -> list[Any]:
            ids["gtm"] = _tool_reply(call)["assignment_id"]
            return [
                function_call(
                    "delegate_agent",
                    {
                        "profile_id": profiles["QA"],
                        "objective": "Review the saved brief bytes",
                        "expected_result": "Independent QA verdict",
                        "acceptance_check": "Only source-backed claims",
                        "acceptance_kind": "qa_review",
                        "depends_on": [ids["gtm"]],
                    },
                    call_id="delegate-qa",
                )
            ]

        def report_artifact(call: Any) -> list[Any]:
            receipt = _tool_reply(call)
            ids.update({"artifact": receipt["result"]["artifact_id"], "run": receipt["run_id"]})
            return [
                function_call(
                    "report_result",
                    {
                        "body": "Source-backed brief",
                        "artifact_id": ids["artifact"],
                        "artifact_run_id": ids["run"],
                    },
                    call_id="report-artifact",
                )
            ]

        models.extend(
            [
                ScriptedModel(
                    [
                        _step(
                            "delegate_agent",
                            {
                                "profile_id": profiles["GTM"],
                                "objective": "Write the brief from saved source facts",
                                "expected_result": "Exact markdown artifact",
                                "acceptance_check": "Only source-backed claims",
                                "acceptance_kind": "artifact",
                            },
                        ),
                        ModelStep.respond(delegate_qa),
                        ModelStep(
                            output=[assistant_message("Artifact and QA assigned after checkpoint 104.")]
                        ),
                    ]
                ),
                ScriptedModel(
                    [
                        _step(
                            "role_tool",
                            {
                                "tool_id": "write_text_artifact_v1",
                                "arguments": {"kind": "markdown", "content": GOOD},
                            },
                        ),
                        ModelStep.respond(report_artifact),
                        ModelStep(output=[assistant_message("Artifact saved.")]),
                    ]
                ),
                ScriptedModel(
                    [
                        _step(
                            "report_result", {"body": "Passed the exact artifact bytes.", "verdict": "passed"}
                        ),
                        ModelStep(output=[assistant_message("Independent QA passed.")]),
                    ]
                ),
            ]
        )
        _queue_continuation(client, base)
        for _ in range(3):
            assert (worker.run_next(pid) or {})["outcome"] == "done"
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
        models.append(
            ScriptedModel(
                [
                    _step(
                        "submit_completion",
                        {
                            "body": "Brief and exact-byte QA ready.",
                            "artifact_id": ids["artifact"],
                            "artifact_run_id": ids["run"],
                        },
                    ),
                    ModelStep(output=[assistant_message("Ready for operator acceptance.")]),
                ]
            )
        )
        assert (worker.run_next(pid) or {})["outcome"] == "done"
        preview = client.get(
            f"/api/projects/{project}/runs/{ids['run']}/workspace-artifacts/{ids['artifact']}"
        )
        assert preview.status_code == 200 and preview.json()["content"] == GOOD
        assert preview.json()["sha256"] == hashlib.sha256(GOOD.encode()).hexdigest()
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        root = next(w for w in orders if w["id"] == oid)
        assert root["status"] == "needs_review"
        accepted = client.post(
            f"/api/projects/{project}/work-orders/{oid}/accept",
            json={
                "expected_version": root["version"],
                "reason": "Fixture verified exact bytes and independent QA.",
            },
            headers=ORIGIN,
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "done", accepted.text
        final = client.get(base).json()
        assert final["session"]["status"] == "closed"
        assert len(final["attempts"]) == 107
        assert len(final["assignments"]) == 2
        assert worker.run_next(pid) is None and models == []
    gateway.engine.dispose()


def test_long_goal_retains_per_attempt_bound_restart_uncertainty_and_cancel(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    gateway = ToolGateway(create_engine(settings.database_url), "fixture-worker", live_text_settings=settings)
    models: list[ScriptedModel] = []
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, _profiles, base = activate_fixture(client)
        pid = UUID(project)
        worker = _scripted_worker(gateway, settings, models)
        _confirmed_turns(client, base, pid, worker, models, 0, 31)

        # An SDK attempt can reach its own tool-loop ceiling without finishing the goal.
        bounded = settings.model_copy(update={"team_max_model_calls_per_turn": 2})
        models.append(
            ScriptedModel(
                [
                    _step("publish_update", {"body": "Bounded check one."}),
                    ModelStep(
                        output=[
                            function_call("publish_update", {"body": "Bounded check two."}, call_id="call_1")
                        ]
                    ),
                ]
            )
        )
        _queue_continuation(client, base)
        failure = _scripted_worker(gateway, bounded, models).run_next(pid)
        assert failure is not None and failure["outcome"] == "failed" and failure["next_attempt_id"] is None
        assert client.get(base).json()["attempts"][-1]["reason"] == "turn_limit_reached"
        assert worker.run_next(pid) is None
        _confirmed_turns(client, base, pid, worker, models, 32, 33)

        # Simulate a lost worker after a persisted claim, without replaying an external action.
        lost_id = _queue_continuation(client, base)
        lost = TaskSessionAttempts(gateway).claim_next(pid)
        assert lost is not None and str(lost["id"]) == lost_id
        session_id = str(lost["session_id"])
    gateway.engine.dispose()

    gateway = ToolGateway(
        create_engine(settings.database_url), "fixture-restarted", live_text_settings=settings
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        attempts = TaskSessionAttempts(gateway)
        assert attempts.claim_next(pid) is None
        future = datetime.now(UTC) + timedelta(seconds=settings.onefoundry_max_elapsed_seconds + 1)
        assert attempts.expire_stale(settings.onefoundry_max_elapsed_seconds, pid, now=future) == 1
        assert attempts.expire_stale(settings.onefoundry_max_elapsed_seconds, pid, now=future) == 0
        view = client.get(base).json()
        assert view["attempts"][-1]["id"] == lost_id
        assert view["attempts"][-1]["status"] == "unknown"
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 409
        )
        order = next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)
        assert order["status"] == "running"
        assert (
            client.post(
                f"{base}/cancel",
                json={"expected_version": order["version"], "client_action_id": str(uuid4())},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        acknowledged = client.post(
            f"{base}/sessions/{session_id}/attempts/{lost_id}/acknowledge",
            json={
                "client_action_id": str(uuid4()),
                "decision": "abandon_unverified",
                "reason": "Fixture worker died; preserve uncertainty and do not replay.",
            },
            headers=ORIGIN,
        )
        assert acknowledged.status_code == 200, acknowledged.text
        worker = _scripted_worker(gateway, settings, models)
        _confirmed_turns(client, base, pid, worker, models, 34, 35)
        queued_id = _queue_continuation(client, base)
        order = next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)
        cancel_data = {"expected_version": order["version"], "client_action_id": str(uuid4())}
        canceled = client.post(f"{base}/cancel", json=cancel_data, headers=ORIGIN)
        assert canceled.status_code == 200 and canceled.json()["queued_canceled"] == 1, canceled.text
        assert client.post(f"{base}/cancel", json=cancel_data, headers=ORIGIN).json() == canceled.json()
        final = client.get(base).json()
        assert final["session"]["status"] == "closed"
        assert (
            final["attempts"][-1]["id"] == queued_id
            and final["attempts"][-1]["reason"] == "operator_canceled"
        )
        assert next(a for a in final["attempts"] if a["id"] == lost_id)["status"] == "unknown"
        assert worker.run_next(pid) is None and models == []
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 409
        )
    gateway.engine.dispose()


def test_long_goal_read_keeps_latest_sequence_and_accepts_next_message(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, _profiles, base = activate_fixture(client)
        opened = client.get(base).json()
        session = opened["session"]
        engine = create_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_session_messages "
                    "(id,project_id,actor_id,root_work_order_id,session_id,sequence,kind,body) "
                    "VALUES (:id,:p,:actor,:root,:s,:sequence,'operator',:body)"
                ),
                [
                    {
                        "id": uuid4(),
                        "p": UUID(project),
                        "actor": settings.operator_actor_id,
                        "root": UUID(session["root_work_order_id"]),
                        "s": UUID(session["id"]),
                        "sequence": number,
                        "body": f"Persisted fixture message {number}.",
                    }
                    for number in range(1, 502)
                ],
            )
        read = client.get(base).json()
        assert read["messages"][-1]["sequence"] == 501
        assert len(read["messages"]) == 500
        assert read["history_omitted"]["messages"] == 1
        sent = client.post(
            f"{base}/messages",
            json={
                "client_message_id": str(uuid4()),
                "expected_sequence": read["messages"][-1]["sequence"],
                "body": "Continue the same long goal.",
            },
            headers=ORIGIN,
        )
        assert sent.status_code == 202, sent.text
        assert sent.json()["sequence"] == 502
        assert client.get(base).json()["messages"][-1]["sequence"] == 502
        assert client.get(base).json()["history_omitted"]["messages"] == 2
        engine.dispose()


def test_long_goal_read_retains_old_uncertainty_outside_recent_window(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, _profiles, base = activate_fixture(client)
        session = client.get(base).json()["session"]
        old_attempt, old_tool = uuid4(), uuid4()
        since = datetime(2026, 1, 1, tzinfo=UTC)
        binding = {
            "p": UUID(project),
            "actor": "fixture",
            "root": UUID(session["root_work_order_id"]),
            "s": UUID(session["id"]),
        }
        engine = create_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_session_attempts "
                    "(id,project_id,actor_id,root_work_order_id,session_id,status,created_at,finished_at) "
                    "VALUES (:id,:p,:actor,:root,:s,:status,:at,:at)"
                ),
                [
                    {
                        **binding,
                        "id": old_attempt if n == 0 else uuid4(),
                        "status": "unknown" if n == 0 else "done",
                        "at": since + timedelta(seconds=n),
                    }
                    for n in range(502)
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO task_session_tool_calls "
                    "(id,project_id,actor_id,root_work_order_id,session_id,attempt_id,provider_call_id,"
                    "tool_name,arguments_sha256,status,result,created_at,finished_at) "
                    "VALUES (:id,:p,:actor,:root,:s,:a,:call,'onefoundry.publish_update',:digest,:status,"
                    "CAST(:result AS jsonb),:at,:finish)"
                ),
                [
                    {
                        **binding,
                        "id": old_tool if n == 0 else uuid4(),
                        "a": old_attempt,
                        "call": f"fixture-{n}",
                        "digest": "a" * 64,
                        "status": "started" if n == 0 else "done",
                        "result": None if n == 0 else "{}",
                        "at": since + timedelta(seconds=n),
                        "finish": None if n == 0 else since + timedelta(seconds=n),
                    }
                    for n in range(502)
                ],
            )
        read = client.get(base).json()
        assert len(read["attempts"]) == 501 and len(read["tool_calls"]) == 501
        assert read["attempts"][0]["id"] == str(old_attempt)
        assert read["attempts"][0]["unknown_acknowledged"] is False
        assert read["tool_calls"][0]["id"] == str(old_tool)
        assert read["tool_calls"][0]["status"] == "started"
        assert read["history_omitted"] == {
            "messages": 0,
            "attempts": 1,
            "tool_calls": 1,
            "decision_causes": 0,
        }
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 409
        )
        engine.dispose()


def test_automatic_team_progress_exceeds_lifetime_chief_and_assignment_limits_after_restart(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    gateway = ToolGateway(create_engine(settings.database_url), "fixture-worker", live_text_settings=settings)
    models: list[ScriptedModel] = []
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, profiles, base = activate_fixture(client)
        pid = UUID(project)
        _queue_continuation(client, base)
    # Each cause, queue, SDK turn, result and continuation is persisted, not a custom production loop.
    for batch in range(12):
        if batch == 6:
            gateway.engine.dispose()
            gateway = ToolGateway(
                create_engine(settings.database_url), "fixture-restarted", live_text_settings=settings
            )
        worker = _scripted_worker(gateway, settings, models)
        models.append(
            ScriptedModel(
                [
                    _step(
                        "delegate_agent",
                        {
                            "profile_id": profiles["Researcher"],
                            "objective": f"Verify source checkpoint {batch}",
                            "expected_result": "Verified checkpoint evidence",
                            "acceptance_check": "Only supplied source facts",
                        },
                    ),
                    ModelStep(output=[assistant_message(f"Checkpoint {batch} assigned.")]),
                ]
            )
        )
        assert (worker.run_next(pid) or {})["outcome"] == "done"
        models.append(
            ScriptedModel(
                [
                    _step("report_result", {"body": f"Source checkpoint {batch}: {FACTS}"}),
                    ModelStep(output=[assistant_message(f"Evidence checkpoint {batch} saved.")]),
                ]
            )
        )
        assert (worker.run_next(pid) or {})["outcome"] == "done"
        decision = TaskSessionDecisions(gateway).dispatch_next(pid)
        assert decision is not None, f"Healthy progress was stopped at batch {batch}"
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is None
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            current = client.get(base).json()
            assert len(current["assignments"]) == batch + 1
            assert all(a["status"] == "satisfied" for a in current["assignments"])
    models.append(
        ScriptedModel(
            [
                ModelStep(
                    output=[
                        assistant_message("Twelve new evidence checkpoints retained; next stage pending.")
                    ]
                )
            ]
        )
    )
    assert (_scripted_worker(gateway, settings, models).run_next(pid) or {})["outcome"] == "done"
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        current = client.get(base).json()
        assert len(current["decision_causes"]) == 12
        assert len(current["assignments"]) == 12 and len(current["attempts"]) == 13
        assert all(a["status"] == "done" for a in current["attempts"])
        assert current["session"]["status"] != "waiting_for_operator"
        orders = client.get(f"/api/projects/{project}/work-orders").json()
        assert next(w for w in orders if w["id"] == oid)["status"] == "running"
    assert models == [] and TaskSessionDecisions(gateway).dispatch_next(pid) is None
    gateway.engine.dispose()


@pytest.mark.parametrize("evidence_kind", ["text_report", "artifact"])
def test_repeated_team_evidence_stops_without_progress_and_operator_checkpoint_resumes(
    tmp_path: Path,
    evidence_kind: str,
) -> None:
    settings = settings_for(tmp_path)
    gateway = ToolGateway(create_engine(settings.database_url), "fixture-worker", live_text_settings=settings)
    models: list[ScriptedModel] = []
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _, profiles, base = activate_fixture(client)
        pid = UUID(project)
        _queue_continuation(client, base)
        worker = _scripted_worker(gateway, settings, models)
        for batch in range(settings.team_max_chief_decisions_per_order + 1):
            models.append(
                ScriptedModel(
                    [
                        _step(
                            "delegate_agent",
                            {
                                "profile_id": profiles[
                                    "GTM" if evidence_kind == "artifact" else "Researcher"
                                ],
                                "acceptance_kind": evidence_kind,
                                "objective": "Repeat the same check",
                                "expected_result": "Same supplied facts",
                                "acceptance_check": "Only source facts",
                            },
                        ),
                        ModelStep(output=[assistant_message("Check assigned.")]),
                    ]
                )
            )
            assert (worker.run_next(pid) or {})["outcome"] == "done"
            if evidence_kind == "artifact":

                def report_saved(call: Any, checkpoint: int = batch) -> list[Any]:
                    receipt = _tool_reply(call)
                    return [
                        function_call(
                            "report_result",
                            {
                                "body": f"Repeated bytes, receipt {checkpoint}",
                                "artifact_id": receipt["result"]["artifact_id"],
                                "artifact_run_id": receipt["run_id"],
                            },
                            call_id=f"report-{checkpoint}",
                        )
                    ]

                models.append(
                    ScriptedModel(
                        [
                            _step(
                                "role_tool",
                                {
                                    "tool_id": "write_text_artifact_v1",
                                    "arguments": {"kind": "markdown", "content": GOOD},
                                },
                            ),
                            ModelStep.respond(report_saved),
                            ModelStep(output=[assistant_message("Same artifact evidence saved.")]),
                        ]
                    )
                )
            else:
                models.append(
                    ScriptedModel(
                        [
                            _step("report_result", {"body": FACTS}),
                            ModelStep(output=[assistant_message("Same evidence saved.")]),
                        ]
                    )
                )
            assert (worker.run_next(pid) or {})["outcome"] == "done"
            if batch < settings.team_max_chief_decisions_per_order:
                assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
            else:
                assert TaskSessionDecisions(gateway).dispatch_next(pid) is None
        current = client.get(base).json()
        assert current["session"]["status"] == "waiting_for_operator"
        assert worker.run_next(pid) is None and models == []
        assert (
            TaskSessionDecisions(gateway).dispatch(pid, UUID(current["session"]["root_work_order_id"]))
            is None
        )
        _queue_continuation(client, base)
        models.append(
            ScriptedModel(
                [
                    ModelStep(
                        output=[assistant_message("Operator checkpoint confirmed; saved evidence retained.")]
                    )
                ]
            )
        )
        assert (worker.run_next(pid) or {})["outcome"] == "done"
        assert client.get(base).json()["session"]["status"] != "waiting_for_operator"
    gateway.engine.dispose()


@pytest.mark.parametrize("summary_size", [51, 11000])
def test_saved_large_artifact_report_preserves_exact_owner_content(tmp_path: Path, summary_size: int) -> None:
    settings = settings_for(tmp_path)
    gateway = ToolGateway(create_engine(settings.database_url), "fixture-worker", live_text_settings=settings)
    models: list[ScriptedModel] = []
    content = "# Checklist\n" + ("Verified source fact; finished media review pending.\n" * 340)
    assert len(content) > 16000
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _, profiles, base = activate_fixture(client)
        pid = UUID(project)
        worker = _scripted_worker(gateway, settings, models)
        _queue_continuation(client, base)
        models.append(
            ScriptedModel(
                [
                    _step(
                        "delegate_agent",
                        {
                            "profile_id": profiles["GTM"],
                            "acceptance_kind": "artifact",
                            "objective": "Save the source checklist",
                            "expected_result": "Exact artifact",
                            "acceptance_check": "Read back owner bytes without truncation",
                        },
                    ),
                    ModelStep(output=[assistant_message("Checklist assigned.")]),
                ]
            )
        )
        assert (worker.run_next(pid) or {})["outcome"] == "done"

        def report_saved(call: Any) -> list[Any]:
            receipt = _tool_reply(call)
            return [
                function_call(
                    "report_result",
                    {
                        "body": "S" * summary_size,
                        "artifact_id": receipt["result"]["artifact_id"],
                        "artifact_run_id": receipt["run_id"],
                    },
                    call_id="report-exact",
                )
            ]

        def finish(call: Any) -> list[Any]:
            assert _tool_reply(call)["status"] == "saved"
            return [assistant_message("Checklist saved; no finished-video claim.")]

        models.append(
            ScriptedModel(
                [
                    _step(
                        "role_tool",
                        {
                            "tool_id": "write_text_artifact_v1",
                            "arguments": {"kind": "markdown", "content": content},
                        },
                    ),
                    ModelStep.respond(report_saved),
                    ModelStep.respond(finish),
                ]
            )
        )
        assert (worker.run_next(pid) or {})["outcome"] == "done"
        view = client.get(base).json()
        assignment = view["assignments"][0]
        assert assignment["status"] == "satisfied"
        child = client.get(f"{base}/sessions/{assignment['child_session_id']}").json()
        saved = json.loads(
            next(m["body"] for m in child["messages"] if m["id"] == assignment["result_message_id"])
        )
        assert saved["artifact"]["content"] == content
        assert saved["artifact"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
        assert len(saved["summary"]) == summary_size
        assert all(t["status"] == "done" for t in child["tool_calls"])
    gateway.engine.dispose()
