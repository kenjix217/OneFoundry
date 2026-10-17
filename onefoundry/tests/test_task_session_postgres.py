"""Team session ledger reads never launch a model or turn historical prose into actions."""

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx2 as httpx
import openai
import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.profile_text_runtime import ProfileTextRuntime
from onefoundry.settings import Settings
from onefoundry.task_session_assignments import DelegateRequest, TaskSessionAssignments
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_runs import bind as bind_team_run
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from runner.codex_app_server import AppServerTimeout, CodexAppServer
from runner.task_session_codex_turn import TaskSessionCodexTurn


@pytest.fixture(autouse=True)
def controlled_team_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", start)


ORIGIN = {"origin": "http://localhost:8765"}


def test_claimed_team_turn_saves_fake_codex_tool_and_final_reply(tmp_path: Path) -> None:
    """A separate fake app-server exercises the real database and scoped tool callback."""
    settings = _settings(tmp_path)
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json,sys\n"
        "def receive(): return json.loads(sys.stdin.readline())\n"
        "def send(value): print(json.dumps(value),flush=True)\n"
        "request=receive(); assert request['method']=='initialize'; "
        "send({'id':request['id'],'result':{}})\n"
        "assert receive()=={'method':'initialized'}\n"
        "request=receive(); assert request['method']=='thread/start'; "
        "assert request['params']['dynamicTools'][0]['name']=='onefoundry'; "
        "send({'id':request['id'],'result':{'thread':{'id':'fake-thread'}}})\n"
        "request=receive(); assert request['method']=='turn/start' and "
        "request['params']['effort']=='xhigh'; "
        "send({'id':request['id'],'result':{'turn':{'id':'fake-turn'}}})\n"
        "update={'method':'item/completed','params':{'threadId':'fake-thread',"
        "'turnId':'fake-turn','item':{'id':'public-plan','type':'agentMessage',"
        "'phase':'commentary','text':'First I will inspect the evidence.'}}}; "
        "send(update); send(update)\n"
        "send({'method':'item/completed','params':{'threadId':'fake-thread',"
        "'turnId':'fake-turn','item':{'id':'private-1','type':'reasoning',"
        "'text':'private reasoning must not be saved'}}})\n"
        "send({'id':31,'method':'item/tool/call','params':{'threadId':'fake-thread',"
        "'turnId':'fake-turn','namespace':'onefoundry','tool':'publish_update',"
        "'callId':'update-1','arguments':{'body':'Plan: inspect the evidence.'}}})\n"
        "response=receive(); assert response['id']==31 and response['result']['success']\n"
        "send({'method':'item/completed','params':{'threadId':'fake-thread',"
        "'turnId':'fake-turn','item':{'id':'message-1','type':'agentMessage',"
        "'phase':'final_answer','text':'Finding saved; next inspect the source.'}}})\n"
        "send({'method':'turn/completed','params':{'turn':{'id':'fake-turn',"
        "'status':'completed'}}})\n"
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id = _order(client, "Codex team adapter")
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        opened = client.post(path, headers=ORIGIN).json()
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(opened["session"]["id"])},
            )
        queued = client.post(f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "team-runner")
        claimed = TaskSessionAttempts(gateway).claim_next(UUID(project_id))
        assert claimed is not None
        result = TaskSessionCodexTurn(gateway, settings).run_claimed(
            UUID(project_id),
            claimed,
            repository=tmp_path,
            model_id="test-model",
            reasoning_effort="xhigh",
            client_factory=lambda callback: CodexAppServer(
                [sys.executable, str(script)],
                cwd=tmp_path,
                environment={"PYTHONUNBUFFERED": "1"},
                dynamic_tool_callback=callback,
            ),
        )
        assert result["outcome"] == "done"
        saved = client.get(path).json()
        assert saved["session"]["provider_thread_id"] == "fake-thread"
        assert saved["attempts"][0]["provider_turn_id"] == "fake-turn"
        assert [row["body"] for row in saved["messages"]] == [
            "First I will inspect the evidence.",
            "Plan: inspect the evidence.",
            "Finding saved; next inspect the source.",
        ]
        assert len(saved["tool_calls"]) == 1
        assert saved["tool_calls"][0]["status"] == "done"
        resumed_script = tmp_path / "fake_codex_resume.py"
        resumed_script.write_text(
            "import json,sys\n"
            "def receive(): return json.loads(sys.stdin.readline())\n"
            "def send(value): print(json.dumps(value),flush=True)\n"
            "request=receive(); send({'id':request['id'],'result':{}})\n"
            "assert receive()=={'method':'initialized'}\n"
            "request=receive(); assert request['method']=='thread/resume' and "
            "request['params']['threadId']=='fake-thread'; "
            "send({'id':request['id'],'result':{'thread':{'id':'fake-thread'}}})\n"
            "request=receive(); assert request['method']=='turn/start' and "
            "request['params']['effort']=='xhigh'; "
            "send({'id':request['id'],'result':{'turn':{'id':'fake-turn-2'}}})\n"
            "send({'method':'item/completed','params':{'threadId':'fake-thread',"
            "'turnId':'fake-turn-2','item':{'id':'message-2','type':'agentMessage',"
            "'phase':'final_answer','text':'Continued in the saved session.'}}})\n"
            "send({'method':'turn/completed','params':{'turn':{'id':'fake-turn-2',"
            "'status':'completed'}}})\n"
        )
        continued = client.post(f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert continued.status_code == 202, continued.text
        second_claim = TaskSessionAttempts(gateway).claim_next(UUID(project_id))
        assert second_claim is not None
        assert second_claim["session_id"] == claimed["session_id"]
        assert second_claim["provider_thread_id"] == "fake-thread"
        second_result = TaskSessionCodexTurn(gateway, settings).run_claimed(
            UUID(project_id),
            second_claim,
            repository=tmp_path,
            model_id="test-model",
            reasoning_effort="xhigh",
            client_factory=lambda callback: CodexAppServer(
                [sys.executable, str(resumed_script)],
                cwd=tmp_path,
                environment={"PYTHONUNBUFFERED": "1"},
                dynamic_tool_callback=callback,
            ),
        )
        assert second_result["outcome"] == "done"
        resumed = client.get(path).json()
        assert [row["status"] for row in resumed["attempts"]] == ["done", "done"]
        assert resumed["messages"][-1]["body"] == "Continued in the saved session."
        interrupted_receipt, interrupted_message = uuid4(), uuid4()
        with engine.begin() as connection:
            sequence = connection.scalar(
                text(
                    "SELECT coalesce(max(sequence),0)+1 FROM task_session_messages "
                    "WHERE project_id=:p AND session_id=:s"
                ),
                {"p": UUID(project_id), "s": claimed["session_id"]},
            )
            connection.execute(
                text(
                    "INSERT INTO task_session_tool_calls "
                    "(id,project_id,actor_id,root_work_order_id,session_id,attempt_id,"
                    "provider_call_id,tool_name,arguments_sha256) VALUES "
                    "(:id,:p,:actor,:root,:session,:attempt,:call,'onefoundry.publish_update',:digest)"
                ),
                {
                    "id": interrupted_receipt,
                    "p": UUID(project_id),
                    "actor": f"profile:{opened['session']['profile_id']}",
                    "root": UUID(order_id),
                    "session": claimed["session_id"],
                    "attempt": second_claim["id"],
                    "call": str(interrupted_receipt),
                    "digest": hashlib.sha256(b"{}").hexdigest(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO task_session_messages "
                    "(id,project_id,actor_id,root_work_order_id,session_id,client_message_id,"
                    "sequence,kind,body) VALUES "
                    "(:id,:p,:actor,:root,:session,:receipt,:sequence,'agent',:body)"
                ),
                {
                    "id": interrupted_message,
                    "p": UUID(project_id),
                    "actor": f"profile:{opened['session']['profile_id']}",
                    "root": UUID(order_id),
                    "session": claimed["session_id"],
                    "receipt": interrupted_receipt,
                    "sequence": sequence,
                    "body": "Saved after interrupted receipt.",
                },
            )
        missing_script = tmp_path / "fake_codex_missing_rollout.py"
        missing_script.write_text(
            "import json,sys\n"
            "def receive(): return json.loads(sys.stdin.readline())\n"
            "def send(value): print(json.dumps(value),flush=True)\n"
            "request=receive(); send({'id':request['id'],'result':{}})\n"
            "assert receive()=={'method':'initialized'}\n"
            "request=receive(); assert request['method']=='thread/resume'; "
            "send({'id':request['id'],'error':{'code':-32600,"
            "'message':'no rollout found for thread id fake-thread'}})\n"
            "request=receive(); assert request['method']=='thread/start'; "
            "send({'id':request['id'],'result':{'thread':{'id':'replacement-thread'}}})\n"
            "request=receive(); assert request['method']=='turn/start' and "
            "'Saved after interrupted receipt.' in request['params']['input'][0]['text']; "
            "send({'id':request['id'],'result':{'turn':{'id':'replacement-turn'}}})\n"
            "send({'method':'item/completed','params':{'threadId':'replacement-thread',"
            "'turnId':'replacement-turn','item':{'id':'message-3','type':'agentMessage',"
            "'phase':'final_answer','text':'Continued from saved OneFoundry records.'}}})\n"
            "send({'method':'turn/completed','params':{'turn':{'id':'replacement-turn',"
            "'status':'completed'}}})\n"
        )
        assert (
            client.post(
                f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        replacement_claim = TaskSessionAttempts(gateway).claim_next(UUID(project_id))
        assert replacement_claim is not None and replacement_claim["provider_thread_id"] == "fake-thread"
        replacement = TaskSessionCodexTurn(gateway, settings).run_claimed(
            UUID(project_id),
            replacement_claim,
            repository=tmp_path,
            model_id="test-model",
            client_factory=lambda callback: CodexAppServer(
                [sys.executable, str(missing_script)],
                cwd=tmp_path,
                environment={"PYTHONUNBUFFERED": "1"},
                dynamic_tool_callback=callback,
            ),
        )
        assert replacement["outcome"] == "done"
        recovered = client.get(path).json()
        assert recovered["session"]["provider_thread_id"] == "replacement-thread"
        assert [row["status"] for row in recovered["attempts"]] == ["done", "done", "done"]
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM task_session_tool_calls WHERE project_id=:p AND id=:id"),
                    {"p": UUID(project_id), "id": interrupted_receipt},
                )
                == "done"
            )
        engine.dispose()


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        team_worker_enabled=True,
    )


def _order(client: TestClient, name: str) -> tuple[str, str, str]:
    project_response = client.post("/api/projects", json={"name": name}, headers=ORIGIN)
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]
    base = f"/api/projects/{project_id}"
    for profile in client.get(f"{base}/profiles").json():
        changed = client.patch(
            f"{base}/profiles/{profile['id']}", json={"autonomy_level": "supervised"}, headers=ORIGIN
        )
        assert changed.status_code == 200, changed.text
    chief = next(
        profile for profile in client.get(f"{base}/profiles").json() if profile["name"] == "Chief of Staff"
    )
    order_response = client.post(
        f"{base}/work-orders",
        json={
            "title": "Continue the project",
            "instructions": "Use the current evidence",
            "profile_id": chief["id"],
        },
        headers=ORIGIN,
    )
    assert order_response.status_code == 201, order_response.text
    ready = client.post(f"{base}/work-orders/{order_response.json()['id']}/ready", headers=ORIGIN)
    assert ready.status_code == 200, ready.text
    return project_id, chief["id"], order_response.json()["id"]


def test_session_open_is_idempotent_and_project_bound(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, order_id = _order(client, "Team ledger")
        other_project, _other_profile, _other_order = _order(client, "Other team ledger")
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        assert client.get(path).status_code == 404
        first = client.post(path, headers=ORIGIN)
        assert first.status_code == 201, first.text
        view = first.json()
        assert view["session"]["kind"] == "main"
        assert view["session"]["profile_id"] == profile_id
        assert view["session"]["root_work_order_id"] == order_id
        assert view["messages"] == []
        assert view["attempts"] == []
        assert view["children"] == []
        second = client.post(path, headers=ORIGIN)
        assert second.status_code == 200
        assert second.json()["session"]["id"] == view["session"]["id"]
        assert client.get(path).json() == second.json()
        assert (
            client.get(f"/api/projects/{other_project}/work-orders/{order_id}/team-session").status_code
            == 404
        )
    with create_engine(settings.database_url).connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM task_sessions WHERE project_id=:p AND work_order_id=:o"),
                {"p": UUID(project_id), "o": UUID(order_id)},
            )
            == 1
        )


def test_existing_child_order_opens_under_parent_without_model_turn(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, parent_id = _order(client, "Child ledger")
        child_id = uuid4()
        with create_engine(settings.database_url).begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO work_orders "
                    "(id,project_id,actor_id,title,instructions,profile_id,status,"
                    "parent_work_order_id,chain_depth) VALUES "
                    "(:id,:p,'operator','Child research','Report one result',:profile,'ready',:parent,1)"
                ),
                {
                    "id": child_id,
                    "p": UUID(project_id),
                    "profile": UUID(profile_id),
                    "parent": UUID(parent_id),
                },
            )
        child_path = f"/api/projects/{project_id}/work-orders/{child_id}/team-session"
        child = client.post(child_path, headers=ORIGIN)
        assert child.status_code == 201, child.text
        assert child.json()["session"]["kind"] == "child"
        parent = client.get(f"/api/projects/{project_id}/work-orders/{parent_id}/team-session")
        assert parent.status_code == 200
        parent_session_id = parent.json()["session"]["id"]
        assert child.json()["session"]["parent_session_id"] == parent_session_id
        assert [item["id"] for item in parent.json()["children"]] == [child.json()["session"]["id"]]
        with create_engine(settings.database_url).connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"),
                    {"p": UUID(project_id)},
                )
                == 0
            )


def test_legacy_proposal_import_is_history_not_a_new_assignment(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, order_id = _order(client, "Legacy team history")
        conversation_id, turn_id = uuid4(), uuid4()
        proposal = '{"reply":"I will ask Researcher.","decision":"delegate_team"}'
        operator_text = "Assign the research."
        with create_engine(settings.database_url).begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_conversations "
                    "(id,project_id,actor_id,work_order_id,profile_id) "
                    "VALUES (:id,:p,'operator',:o,:profile)"
                ),
                {
                    "id": conversation_id,
                    "p": UUID(project_id),
                    "o": UUID(order_id),
                    "profile": UUID(profile_id),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO task_conversation_turns "
                    "(id,project_id,actor_id,conversation_id,client_turn_id,sequence,status,"
                    "operator_text,operator_sha256,prompt_sha256,assistant_text,assistant_sha256,"
                    "assistant_actor_id,profile_id,account_id,provider,model_id,credential_revision,"
                    "input_tokens,output_tokens,finished_at) VALUES "
                    "(:id,:p,'operator',:chat,:client,1,'done',:operator,:operator_hash,:prompt_hash,"
                    ":assistant,:assistant_hash,:agent,:profile,:account,'codex','gpt-test',1,40,20,now())"
                ),
                {
                    "id": turn_id,
                    "p": UUID(project_id),
                    "chat": conversation_id,
                    "client": uuid4(),
                    "operator": operator_text,
                    "operator_hash": hashlib.sha256(operator_text.encode()).hexdigest(),
                    "prompt_hash": hashlib.sha256(b"prompt").hexdigest(),
                    "assistant": proposal,
                    "assistant_hash": hashlib.sha256(proposal.encode()).hexdigest(),
                    "agent": f"profile:{profile_id}",
                    "profile": UUID(profile_id),
                    "account": uuid4(),
                },
            )
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        response = client.post(path, headers=ORIGIN)
        assert response.status_code == 201, response.text
        view = response.json()
        assert [message["kind"] for message in view["messages"]] == ["legacy_operator", "legacy_agent"]
        assert view["messages"][1]["body"] == proposal
        assert view["attempts"][0]["status"] == "done"
        assert view["attempts"][0]["cost_usd"] is None
        assert client.post(path, headers=ORIGIN).json()["messages"] == view["messages"]
        assert len(client.get(f"/api/projects/{project_id}/work-orders").json()) == 1
    bootstrap_url = settings.database_url.set(
        username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
    )
    with create_engine(bootstrap_url).connect() as connection:
        with pytest.raises(DBAPIError, match="append only"):
            with connection.begin_nested():
                connection.execute(
                    text("UPDATE task_session_messages SET body='changed' WHERE id=:id"),
                    {"id": UUID(view["messages"][0]["id"])},
                )
        with pytest.raises(DBAPIError, match="immutable"):
            with connection.begin_nested():
                connection.execute(
                    text("UPDATE task_session_attempts SET status='failed' WHERE id=:id"),
                    {"id": UUID(view["attempts"][0]["id"])},
                )


def test_team_send_is_immediate_idempotent_and_keeps_one_active_attempt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id = _order(client, "Queue team messages")
        other_project, _other_profile, _other_order = _order(client, "Isolated messages")
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        opened = client.post(path, headers=ORIGIN)
        assert opened.status_code == 201
        old_chat = client.post(
            f"/api/projects/{project_id}/work-orders/{order_id}/conversation", headers=ORIGIN
        )
        assert old_chat.status_code in (200, 201), old_chat.text
        first_id = str(uuid4())
        first = {"client_message_id": first_id, "expected_sequence": 0, "body": "Please start work."}
        assert client.post(f"{path}/messages", json=first, headers=ORIGIN).status_code == 409
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text(
                    "UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND work_order_id=:o"
                ),
                {"p": UUID(project_id), "o": UUID(order_id)},
            )
        sent = client.post(f"{path}/messages", json=first, headers=ORIGIN)
        assert sent.status_code == 202, sent.text
        old_send = client.post(
            f"/api/projects/{project_id}/work-orders/{order_id}/conversation/turns",
            json={
                "conversation_id": old_chat.json()["id"],
                "client_turn_id": str(uuid4()),
                "expected_sequence": 0,
                "text": "Legacy send should stop at cutover.",
            },
            headers=ORIGIN,
        )
        assert old_send.status_code == 409, old_send.text
        assert sent.json()["state"] == "queued"
        assert sent.json()["sequence"] == 1
        assert sent.json()["attempt_id"] is not None
        repeated = client.post(f"{path}/messages", json=first, headers=ORIGIN)
        assert repeated.status_code == 200
        assert repeated.json() == sent.json()
        changed = dict(first, body="Changed message")
        assert client.post(f"{path}/messages", json=changed, headers=ORIGIN).status_code == 409
        stale = {"client_message_id": str(uuid4()), "expected_sequence": 0, "body": "Second"}
        assert client.post(f"{path}/messages", json=stale, headers=ORIGIN).status_code == 409
        second = dict(stale, expected_sequence=1)
        coalesced = client.post(f"{path}/messages", json=second, headers=ORIGIN)
        assert coalesced.status_code == 202, coalesced.text
        assert coalesced.json()["state"] == "coalesced"
        assert coalesced.json()["attempt_id"] is None
        assert (
            client.post(
                f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 409
        )
        view = client.get(path).json()
        assert len(view["messages"]) == 2
        assert len(view["attempts"]) == 1
        assert view["session"]["status"] == "working"
        assert view["session"]["execution_mode"] == "team"
        assert (
            client.get(f"/api/projects/{other_project}/work-orders/{order_id}/team-session").status_code
            == 404
        )
        engine = create_engine(settings.database_url)
        attempts = TaskSessionAttempts(ToolGateway(engine, "operator"))
        claimed = attempts.claim_next(UUID(project_id))
        assert claimed is not None
        assert str(claimed["id"]) == sent.json()["attempt_id"]
        assert claimed["input_sequence"] == 2
        order_state = next(
            item
            for item in client.get(f"/api/projects/{project_id}/work-orders").json()
            if item["id"] == order_id
        )
        assert order_state["status"] == "running"
        TaskSessionContext(ToolGateway(engine, "operator"), settings).prepare(UUID(project_id), claimed["id"])
        attempts.bind_provider_thread(UUID(project_id), claimed["id"], "thread-1")
        attempts.bind_provider_thread(UUID(project_id), claimed["id"], "thread-1")
        attempts.bind_provider_turn(UUID(project_id), claimed["id"], "turn-1")
        attempts.bind_provider_turn(UUID(project_id), claimed["id"], "turn-1")
        with pytest.raises(Conflict, match="provider turn changed"):
            attempts.bind_provider_turn(UUID(project_id), claimed["id"], "other-turn")
        with pytest.raises(Conflict, match="provider thread changed"):
            attempts.bind_provider_thread(UUID(project_id), claimed["id"], "other-thread")
        third = client.post(
            f"{path}/messages",
            json={"client_message_id": str(uuid4()), "expected_sequence": 2, "body": "Also check QA."},
            headers=ORIGIN,
        )
        assert third.status_code == 202, third.text
        assert third.json()["state"] == "coalesced"
        finished = attempts.settle(
            UUID(project_id),
            UUID(sent.json()["attempt_id"]),
            "done",
            public_reply="Plan saved; QA is next.",
            provider="codex",
            model_id="test-model",
            input_tokens=31,
            output_tokens=12,
        )
        assert finished["next_attempt_id"] is not None
        view = client.get(path).json()
        assert [attempt["status"] for attempt in view["attempts"]] == ["done", "queued"]
        assert [message["kind"] for message in view["messages"]] == [
            "operator",
            "operator",
            "operator",
            "agent",
        ]
        assert view["session"]["provider_thread_id"] == "thread-1"
        assert view["attempts"][0]["provider_turn_id"] == "turn-1"
        engine.dispose()
        assert (
            client.post(
                f"/api/projects/{other_project}/work-orders/{order_id}/team-session/messages",
                json={"client_message_id": str(uuid4()), "expected_sequence": 0, "body": "Wrong project"},
                headers=ORIGIN,
            ).status_code
            == 404
        )


def test_continue_queues_once_after_stopped_attempt_without_replaying_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id = _order(client, "Continue same session")
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        opened = client.post(path, headers=ORIGIN).json()
        session_id = UUID(opened["session"]["id"])
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": session_id},
            )
        request = {"client_attempt_id": str(uuid4())}
        queued = client.post(f"{path}/continue", json=request, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        assert queued.json()["message_id"] is None
        assert queued.json()["sequence"] == 0
        assert client.post(f"{path}/continue", json=request, headers=ORIGIN).status_code == 200
        engine = create_engine(settings.database_url)
        attempts = TaskSessionAttempts(ToolGateway(engine, "operator"))
        claimed = attempts.claim_next(UUID(project_id))
        assert claimed is not None and str(claimed["id"]) == queued.json()["attempt_id"]
        TaskSessionContext(ToolGateway(engine, "operator"), settings).prepare(UUID(project_id), claimed["id"])
        attempts.bind_provider_thread(UUID(project_id), claimed["id"], "lost-thread")
        stopped = attempts.settle(
            UUID(project_id),
            UUID(queued.json()["attempt_id"]),
            "unknown",
            reason="provider_outcome_unknown",
        )
        assert stopped["next_attempt_id"] is None
        order_state = next(
            item
            for item in client.get(f"/api/projects/{project_id}/work-orders").json()
            if item["id"] == order_id
        )
        assert order_state["status"] == "running"
        assert (
            client.post(
                f"{path}/sessions/{session_id}/attempts/{claimed['id']}/acknowledge",
                json={
                    "client_action_id": str(uuid4()),
                    "decision": "abandon_unverified",
                    "reason": "Abandon the interrupted test turn without replaying it.",
                },
                headers=ORIGIN,
            ).status_code
            == 200
        )
        new_request = {"client_attempt_id": str(uuid4())}
        next_attempt = client.post(f"{path}/continue", json=new_request, headers=ORIGIN)
        assert next_attempt.status_code == 202, next_attempt.text
        assert next_attempt.json()["attempt_id"] != queued.json()["attempt_id"]
        next_claim = attempts.claim_next(UUID(project_id))
        assert next_claim is not None and next_claim["provider_thread_id"] == "lost-thread"
        absent_receipt, saved_receipt, saved_message = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            for receipt in (absent_receipt, saved_receipt):
                connection.execute(
                    text(
                        "INSERT INTO task_session_tool_calls "
                        "(id,project_id,actor_id,root_work_order_id,session_id,attempt_id,"
                        "provider_call_id,tool_name,arguments_sha256) VALUES "
                        "(:id,:p,:actor,:root,:session,:attempt,:call,'onefoundry.publish_update',:digest)"
                    ),
                    {
                        "id": receipt,
                        "p": UUID(project_id),
                        "actor": f"profile:{_profile_id}",
                        "root": UUID(order_id),
                        "session": session_id,
                        "attempt": UUID(queued.json()["attempt_id"]),
                        "call": str(receipt),
                        "digest": hashlib.sha256(b"{}").hexdigest(),
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO task_session_messages "
                    "(id,project_id,actor_id,root_work_order_id,session_id,client_message_id,"
                    "sequence,kind,body) VALUES (:id,:p,:actor,:root,:session,:receipt,"
                    "(SELECT COALESCE(max(sequence),0)+1 FROM task_session_messages "
                    "WHERE project_id=:p AND session_id=:session),'agent',:body)"
                ),
                {
                    "id": saved_message,
                    "p": UUID(project_id),
                    "actor": f"profile:{_profile_id}",
                    "root": UUID(order_id),
                    "session": session_id,
                    "receipt": saved_receipt,
                    "body": "Saved before receipt settlement.",
                },
            )
        with pytest.raises(Conflict, match="needs reconciliation"):
            attempts.replace_missing_thread(UUID(project_id), next_claim["id"], "lost-thread")
        assert (
            TaskSessionTools(
                ToolGateway(engine, "team-runner"), UUID(project_id), session_id, next_claim["id"]
            ).reconcile_previous_actions()
            == 2
        )
        with engine.connect() as connection:
            receipts = {
                row["id"]: dict(row)
                for row in connection.execute(
                    text(
                        "SELECT id,status,result,reason FROM task_session_tool_calls "
                        "WHERE project_id=:p AND id IN (:absent,:saved)"
                    ),
                    {"p": UUID(project_id), "absent": absent_receipt, "saved": saved_receipt},
                ).mappings()
            }
        assert receipts[absent_receipt]["reason"] == "effect_not_found"
        assert receipts[saved_receipt]["result"] == {"message_id": str(saved_message)}
        attempts.replace_missing_thread(UUID(project_id), next_claim["id"], "lost-thread")
        assert client.get(path).json()["session"]["provider_thread_id"] is None
        preflight = TaskSessionCodexTurn(ToolGateway(engine, "team-runner"), settings).run_claimed(
            UUID(project_id),
            next_claim,
            repository=tmp_path / "missing",
            model_id="test-model",
            client_factory=lambda _callback: pytest.fail("provider launched after failed preflight"),
        )
        assert preflight["outcome"] == "failed"
        view = client.get(path).json()
        assert [attempt["status"] for attempt in view["attempts"]] == ["unknown", "failed"]
        assert view["session"]["id"] == str(session_id)
        engine.dispose()


def test_abandoned_team_turn_expires_unknown_and_can_continue(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id = _order(client, "Abandoned team turn")
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        session = client.post(path, headers=ORIGIN).json()["session"]
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(session["id"])},
            )
        assert (
            client.post(
                f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        attempts = TaskSessionAttempts(ToolGateway(engine, "team-recovery"))
        claimed = attempts.claim_next(UUID(project_id))
        assert claimed is not None
        with pytest.raises(ValueError, match="elapsed bound"):
            attempts.expire_stale(0, UUID(project_id))
        with pytest.raises(ValueError, match="timezone"):
            attempts.expire_stale(600, UUID(project_id), now=datetime(2026, 10, 4))
        assert attempts.expire_stale(600, UUID(project_id)) == 0
        future = datetime.now(UTC) + timedelta(seconds=1200)
        assert attempts.expire_stale(600, UUID(project_id), now=future) == 1
        assert attempts.expire_stale(600, UUID(project_id), now=future) == 0
        saved = client.get(path).json()
        assert saved["session"]["status"] == "open"
        assert saved["attempts"][0]["status"] == "unknown"
        assert saved["attempts"][0]["reason"] == "runner_interrupted"
        order = next(
            row
            for row in client.get(f"/api/projects/{project_id}/work-orders").json()
            if row["id"] == order_id
        )
        assert order["status"] == "running"
        assert (
            client.post(
                f"{path}/sessions/{session['id']}/attempts/{claimed['id']}/acknowledge",
                json={
                    "client_action_id": str(uuid4()),
                    "decision": "abandon_unverified",
                    "reason": "Abandon the expired test turn without replaying it.",
                },
                headers=ORIGIN,
            ).status_code
            == 200
        )
        continuation = client.post(
            f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
        )
        assert continuation.status_code == 202, continuation.text
        assert continuation.json()["attempt_id"] != str(claimed["id"])
        engine.dispose()


def test_lost_turn_start_reply_stays_unknown_for_reconciliation(tmp_path: Path) -> None:
    class LostStart(CodexAppServer):
        def __init__(self) -> None:
            pass

        def __enter__(self) -> "LostStart":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def start_thread(self, **_kwargs: object) -> str:
            return "thread-with-uncertain-turn"

        def start_turn(self, *_args: object, **_kwargs: object) -> str:
            raise AppServerTimeout("turn/start reply lost")

    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id = _order(client, "Lost turn launch")
        path = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        session = client.post(path, headers=ORIGIN).json()["session"]
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(session["id"])},
            )
        assert (
            client.post(
                f"{path}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "team-runner")
        claimed = TaskSessionAttempts(gateway).claim_next(UUID(project_id))
        assert claimed is not None
        result = TaskSessionCodexTurn(gateway, settings).run_claimed(
            UUID(project_id),
            claimed,
            repository=tmp_path,
            model_id="test-model",
            client_factory=lambda _callback: LostStart(),
        )
        assert result["outcome"] == "unknown"
        saved = client.get(path).json()
        assert saved["attempts"][0]["reason"] == "provider_outcome_unknown"
        assert saved["attempts"][0]["provider_turn_id"] is None
        assert saved["session"]["provider_thread_id"] == "thread-with-uncertain-turn"
        assert len(saved["attempts"]) == 1
        engine.dispose()


def test_child_result_releases_only_declared_dependency_without_chief_wakeup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _chief_id, order_id = _order(client, "Team dependency")
        other_project, _other_chief, _other_order = _order(client, "Unrelated team")
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        root = client.post(base, headers=ORIGIN).json()["session"]
        profiles = client.get(f"/api/projects/{project_id}/profiles").json()
        researcher = next(row for row in profiles if row["name"] == "Researcher")
        gtm = next(row for row in profiles if row["name"] == "GTM")
        qa = next(row for row in profiles if row["name"] == "QA")
        other_profiles = client.get(f"/api/projects/{other_project}/profiles").json()
        other_researcher = next(row for row in other_profiles if row["name"] == "Researcher")
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(root["id"])},
            )
        queued = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "operator")
        attempts = TaskSessionAttempts(gateway)
        assignments = TaskSessionAssignments(gateway)
        chief_turn = attempts.claim_next(UUID(project_id))
        assert chief_turn is not None and chief_turn["session_id"] == UUID(root["id"])
        research_request = DelegateRequest(
            client_action_id=uuid4(),
            profile_id=UUID(researcher["id"]),
            objective="Review the product and site",
            expected_result="A supported feature summary",
            acceptance_check="Include a source for each claim",
        )
        with pytest.raises(NotFound, match="target agent profile not found"):
            assignments.delegate(
                UUID(project_id),
                UUID(root["id"]),
                chief_turn["id"],
                DelegateRequest(
                    client_action_id=uuid4(),
                    profile_id=UUID(other_researcher["id"]),
                    objective="Wrong project",
                    expected_result="No output",
                    acceptance_check="No output",
                ),
            )
        research = assignments.delegate(
            UUID(project_id), UUID(root["id"]), chief_turn["id"], research_request
        )
        assert (
            assignments.delegate(UUID(project_id), UUID(root["id"]), chief_turn["id"], research_request)["id"]
            == research["id"]
        )
        script_request = DelegateRequest(
            client_action_id=uuid4(),
            profile_id=UUID(gtm["id"]),
            objective="Write the demo script",
            expected_result="A timed script",
            acceptance_check="Use verified product claims",
            depends_on=(research["id"],),
        )
        script = assignments.delegate(UUID(project_id), UUID(root["id"]), chief_turn["id"], script_request)
        assert script["status"] == "pending"
        unrelated = assignments.delegate(
            UUID(project_id),
            UUID(root["id"]),
            chief_turn["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(qa["id"]),
                objective="Review an unrelated style guide",
                expected_result="A style note",
                acceptance_check="Report one recommendation",
            ),
        )
        waiting_message = client.post(
            f"{base}/sessions/{script['child_session_id']}/messages",
            json={
                "client_message_id": str(uuid4()),
                "expected_sequence": 0,
                "body": "Keep the script concise.",
            },
            headers=ORIGIN,
        )
        assert waiting_message.status_code == 202, waiting_message.text
        assert waiting_message.json()["state"] == "waiting_for_input"
        view = client.get(base).json()
        assert len(view["children"]) == 3
        assert len(view["assignments"]) == 3
        assert view["dependencies"] == [
            {"assignment_id": str(script["id"]), "depends_on_id": str(research["id"])}
        ]
        assert len(view["attempts"]) == 1
        assert client.get(f"{base}/sessions/{research['child_session_id']}").status_code == 200
        assert (
            client.get(
                f"/api/projects/{other_project}/work-orders/{order_id}/team-session/"
                f"sessions/{research['child_session_id']}"
            ).status_code
            == 404
        )
        attempts.settle(UUID(project_id), chief_turn["id"], "done", public_reply="Assignments saved.")
        research_turn = attempts.claim_next(UUID(project_id))
        assert research_turn is not None and research_turn["session_id"] == research["child_session_id"]
        reported = assignments.report_text_result(
            UUID(project_id),
            research["child_session_id"],
            research_turn["id"],
            uuid4(),
            "Verified feature summary with source references.",
        )
        assert reported["released"] == [script["id"]]
        view = client.get(base).json()
        assert {row["id"]: row["status"] for row in view["assignments"]} == {
            str(research["id"]): "satisfied",
            str(script["id"]): "ready",
            str(unrelated["id"]): "ready",
        }
        assert len(view["attempts"]) == 1
        child_view = client.get(f"{base}/sessions/{script['child_session_id']}").json()
        assert len(child_view["attempts"]) == 1
        assert child_view["attempts"][0]["status"] == "queued"
        assert client.get(base).json()["attempts"] == view["attempts"]
        unrelated_turn = attempts.claim_next(UUID(project_id))
        assert unrelated_turn is not None and unrelated_turn["session_id"] == unrelated["child_session_id"]
        assignments.report_text_result(
            UUID(project_id),
            unrelated["child_session_id"],
            unrelated_turn["id"],
            uuid4(),
            "Unrelated style recommendation: use generous spacing.",
        )
        gtm_turn = attempts.claim_next(UUID(project_id))
        assert gtm_turn is not None and gtm_turn["session_id"] == script["child_session_id"]
        snapshot = TaskSessionContext(gateway, settings).prepare(UUID(project_id), gtm_turn["id"])
        assert "Verified feature summary with source references." in snapshot.prompt
        assert "Keep the script concise." in snapshot.prompt
        assert "Unrelated style recommendation" not in snapshot.prompt
        assert snapshot.dependency_ids == (research["id"],)
        stored = client.get(f"{base}/sessions/{script['child_session_id']}").json()
        assert stored["attempts"][0]["context_dependency_ids"] == [str(research["id"])]
        assert stored["attempts"][0]["prompt_sha256"] == snapshot.prompt_sha256
        engine.dispose()


@pytest.mark.parametrize("child_outcome", ["failed", "done", "recovered"])
def test_child_causes_batch_one_chief_turn_and_include_only_confirmed_results(
    tmp_path: Path, child_outcome: str
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _chief_id, order_id = _order(client, f"Team decision {child_outcome}")
        profiles = client.get(f"/api/projects/{project_id}/profiles").json()
        agents = [next(item for item in profiles if item["name"] == name) for name in ("Researcher", "QA")]
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        main = client.post(base, headers=ORIGIN).json()["session"]
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(main["id"])},
            )
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "operator")
        attempts = TaskSessionAttempts(gateway)
        assignments = TaskSessionAssignments(gateway)
        decisions = TaskSessionDecisions(gateway)
        chief_turn = attempts.claim_next(UUID(project_id))
        assert chief_turn is not None
        created = [
            assignments.delegate(
                UUID(project_id),
                UUID(main["id"]),
                chief_turn["id"],
                DelegateRequest(
                    client_action_id=uuid4(),
                    profile_id=UUID(profile["id"]),
                    objective=f"Review {profile['name']} evidence",
                    expected_result="A saved result",
                    acceptance_check="Report one supported finding",
                ),
            )
            for profile in agents
        ]
        attempts.settle(UUID(project_id), chief_turn["id"], "done", public_reply="Assignments saved.")
        for index, assignment in enumerate(created):
            child_turn = attempts.claim_next(UUID(project_id))
            assert child_turn is not None and child_turn["session_id"] == assignment["child_session_id"]
            if child_outcome in ("failed", "recovered"):
                attempts.settle(UUID(project_id), child_turn["id"], "failed", reason="provider_unavailable")
            else:
                assignments.report_text_result(
                    UUID(project_id),
                    assignment["child_session_id"],
                    child_turn["id"],
                    uuid4(),
                    f"Confirmed result {index + 1}.",
                )
                attempts.settle(UUID(project_id), child_turn["id"], "done", public_reply="Result saved.")
        if child_outcome == "recovered":
            resumed = client.post(
                f"{base}/sessions/{created[0]['child_session_id']}/continue",
                json={"client_attempt_id": str(uuid4())},
                headers=ORIGIN,
            )
            assert resumed.status_code == 202, resumed.text
            recovery_turn = attempts.claim_next(UUID(project_id))
            assert recovery_turn is not None and recovery_turn["session_id"] == created[0]["child_session_id"]
            assignments.report_text_result(
                UUID(project_id),
                created[0]["child_session_id"],
                recovery_turn["id"],
                uuid4(),
                "Recovered and verified result.",
            )
            attempts.settle(UUID(project_id), recovery_turn["id"], "done", public_reply="Result saved.")
        with engine.connect() as connection:
            causes = (
                connection.execute(
                    text(
                        "SELECT kind,dispatch_attempt_id,resolved_at FROM task_session_decision_causes "
                        "WHERE project_id=:p AND root_work_order_id=:root"
                    ),
                    {"p": UUID(project_id), "root": UUID(order_id)},
                )
                .mappings()
                .all()
            )
        assert len(causes) == (2 if child_outcome != "done" else 1)
        assert {item["kind"] for item in causes} == (
            {"blocker"} if child_outcome != "done" else {"synthesis"}
        )
        assert all(item["dispatch_attempt_id"] is None for item in causes)
        saved_root = client.get(base).json()
        assert len(saved_root["attempts"]) == 1  # Reading caused no Chief turn.
        assert len(saved_root["decision_causes"]) == len(causes)
        dispatched = decisions.dispatch_next(UUID(project_id))
        assert dispatched is not None and len(dispatched["cause_ids"]) == (
            1 if child_outcome == "recovered" else len(causes)
        )
        assert decisions.dispatch(UUID(project_id), UUID(order_id)) is None
        next_chief = attempts.claim_next(UUID(project_id))
        assert next_chief is not None and next_chief["session_id"] == UUID(main["id"])
        prompt = TaskSessionContext(gateway, settings).prepare(UUID(project_id), next_chief["id"]).prompt
        if child_outcome == "failed":
            assert "Researcher: provider unavailable" in prompt
            assert "QA: provider unavailable" in prompt
            attempts.settle(UUID(project_id), next_chief["id"], "unknown", reason="provider_outcome_unknown")
            acknowledged = client.post(
                f"{base}/sessions/{main['id']}/attempts/{next_chief['id']}/acknowledge",
                json={
                    "client_action_id": str(uuid4()),
                    "decision": "abandon_unverified",
                    "reason": "Abandon the uncertain Chief turn and use confirmed child evidence.",
                },
                headers=ORIGIN,
            )
            assert acknowledged.status_code == 200, acknowledged.text
            continuation = client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            )
            assert continuation.status_code == 202, continuation.text
            resumed_chief = attempts.claim_next(UUID(project_id))
            assert resumed_chief is not None and resumed_chief["session_id"] == UUID(main["id"])
            resumed_prompt = (
                TaskSessionContext(gateway, settings).prepare(UUID(project_id), resumed_chief["id"]).prompt
            )
            assert "Researcher: provider unavailable" in resumed_prompt
            assert "QA: provider unavailable" in resumed_prompt
            attempts.settle(UUID(project_id), resumed_chief["id"], "done", public_reply="Review saved.")
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM task_session_decision_causes WHERE project_id=:p "
                            "AND handled_at IS NOT NULL"
                        ),
                        {"p": UUID(project_id)},
                    )
                    == 2
                )
        elif child_outcome == "recovered":
            assert "Recovered and verified result." in prompt
            assert "QA: provider unavailable" in prompt
            assert "Researcher: provider unavailable" not in prompt
        else:
            assert "Confirmed result 1." in prompt and "Confirmed result 2." in prompt
            assert "Chief decision cause (synthesis)" in prompt
        engine.dispose()


def test_manual_chief_continue_consumes_ready_child_cause(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _chief_id, order_id = _order(client, "Manual Chief review")
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        main = client.post(base, headers=ORIGIN).json()["session"]
        researcher = next(
            profile
            for profile in client.get(f"/api/projects/{project_id}/profiles").json()
            if profile["name"] == "Researcher"
        )
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(main["id"])},
            )
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "operator")
        attempts = TaskSessionAttempts(gateway)
        chief = attempts.claim_next(UUID(project_id))
        assert chief is not None
        assignment = TaskSessionAssignments(gateway).delegate(
            UUID(project_id),
            UUID(main["id"]),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(researcher["id"]),
                objective="Review the evidence",
                expected_result="A saved finding",
                acceptance_check="Report one supported finding",
            ),
        )
        attempts.settle(UUID(project_id), chief["id"], "done", public_reply="Research assigned.")
        child = attempts.claim_next(UUID(project_id))
        assert child is not None and child["session_id"] == assignment["child_session_id"]
        TaskSessionAssignments(gateway).report_text_result(
            UUID(project_id),
            assignment["child_session_id"],
            child["id"],
            uuid4(),
            "Verified research finding.",
        )
        attempts.settle(UUID(project_id), child["id"], "done", public_reply="Finding saved.")
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        manual = attempts.claim_next(UUID(project_id))
        assert manual is not None and manual["session_id"] == UUID(main["id"])
        prompt = TaskSessionContext(gateway, settings).prepare(UUID(project_id), manual["id"]).prompt
        assert "Verified research finding." in prompt
        assert "Chief decision cause (synthesis)" in prompt
        attempts.settle(UUID(project_id), manual["id"], "done", public_reply="Finding reviewed.")
        with engine.connect() as connection:
            cause = (
                connection.execute(
                    text(
                        "SELECT dispatch_attempt_id,handled_at FROM task_session_decision_causes "
                        "WHERE project_id=:p AND root_work_order_id=:root AND kind='synthesis'"
                    ),
                    {"p": UUID(project_id), "root": UUID(order_id)},
                )
                .mappings()
                .one()
            )
        assert cause["dispatch_attempt_id"] == manual["id"]
        assert cause["handled_at"] is not None
        assert TaskSessionDecisions(gateway).dispatch_next(UUID(project_id)) is None
        engine.dispose()


def test_scoped_team_tools_save_idempotent_actions_and_deny_other_projects(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _chief_id, order_id = _order(client, "Team tools")
        other_project, _other_chief, _other_order = _order(client, "Other team tools")
        researcher = next(
            item
            for item in client.get(f"/api/projects/{project_id}/profiles").json()
            if item["name"] == "Researcher"
        )
        other_researcher = next(
            item
            for item in client.get(f"/api/projects/{other_project}/profiles").json()
            if item["name"] == "Researcher"
        )
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        root = client.post(base, headers=ORIGIN).json()["session"]
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(root["id"])},
            )
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "operator")
        attempts = TaskSessionAttempts(gateway)
        chief_turn = attempts.claim_next(UUID(project_id))
        assert chief_turn is not None
        chief_tools = TaskSessionTools(gateway, UUID(project_id), UUID(root["id"]), chief_turn["id"])
        advertised = chief_tools.specifications()[0]["tools"]
        assert {item["name"] for item in advertised} == {
            "publish_update",
            "delegate_agent",
            "read_dependency_result",
            "list_agents",
            "read_project_status",
            "read_work_order",
            "read_agent_session",
            "inspect_capability",
            "read_extension_status",
            "propose_maintenance_target",
            "propose_capability_setup",
            "prepare_extension_release",
            "request_extension",
            "propose_extension_activation",
            "propose_company_knowledge",
        }
        update = chief_tools.invoke(
            "onefoundry.publish_update", {"body": "Plan: research the product."}, "public-1"
        )
        assert update["success"] is True
        assert (
            chief_tools.invoke(
                "onefoundry.publish_update", {"body": "Plan: research the product."}, "public-1"
            )
            == update
        )
        secret = chief_tools.invoke(
            "onefoundry.publish_update", {"body": "token=secret_value_12345"}, "public-secret"
        )
        assert secret["success"] is False
        assert json.loads(secret["contentItems"][0]["text"]) == {"reason": "invalid_tool_input"}
        request = {
            "profile_id": researcher["id"],
            "objective": "Review the product",
            "expected_result": "Supported findings",
            "acceptance_check": "Cite the source",
        }
        assigned = chief_tools.invoke("onefoundry.delegate_agent", request, "delegate-1")
        assert assigned["success"] is True
        assert chief_tools.invoke("onefoundry.delegate_agent", request, "delegate-1") == assigned
        with pytest.raises(Conflict, match="provider tool call identity changed"):
            chief_tools.invoke("onefoundry.delegate_agent", {**request, "objective": "Changed"}, "delegate-1")
        denied = chief_tools.invoke(
            "onefoundry.delegate_agent",
            {**request, "profile_id": other_researcher["id"]},
            "delegate-other-project",
        )
        assert json.loads(denied["contentItems"][0]["text"]) == {"reason": "tool_target_missing"}
        with pytest.raises(Conflict, match="not admitted"):
            chief_tools.invoke("onefoundry.report_result", {"body": "False result"}, "wrong-role")
        root_view = client.get(base).json()
        assert len(root_view["messages"]) == 1
        assert len(root_view["children"]) == 1
        assert sorted(item["status"] for item in root_view["tool_calls"]) == [
            "done",
            "done",
            "failed",
            "failed",
        ]
        assert "secret_value_12345" not in json.dumps(root_view)
        attempts.settle(UUID(project_id), chief_turn["id"], "done", public_reply="Assignment saved.")
        with pytest.raises(Conflict, match="outside an active agent turn"):
            chief_tools.invoke("onefoundry.publish_update", {"body": "Late update"}, "late")
        child_id = UUID(json.loads(assigned["contentItems"][0]["text"])["child_session_id"])
        child_turn = attempts.claim_next(UUID(project_id))
        assert child_turn is not None and child_turn["session_id"] == child_id
        child_tools = TaskSessionTools(gateway, UUID(project_id), child_id, child_turn["id"])
        assert {item["name"] for item in child_tools.specifications()[0]["tools"]} == {
            "publish_update",
            "report_result",
            "read_dependency_result",
            "list_agents",
        }
        result = child_tools.invoke(
            "onefoundry.report_result", {"body": "Confirmed source: current product site."}, "result-1"
        )
        assert result["success"] is True
        assert (
            child_tools.invoke(
                "onefoundry.report_result", {"body": "Confirmed source: current product site."}, "result-1"
            )
            == result
        )
        child_view = client.get(f"{base}/sessions/{child_id}").json()
        assert len(child_view["tool_calls"]) == 1
        assert child_view["tool_calls"][0]["status"] == "done"
        assert child_view["tool_calls"][0]["result"] == json.loads(result["contentItems"][0]["text"])
        assert client.get(base).json()["assignments"][0]["status"] == "satisfied"
        with pytest.raises(DBAPIError, match="team tool call is immutable"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE task_session_tool_calls SET reason='changed' WHERE project_id=:p AND id=:id"
                    ),
                    {"p": UUID(project_id), "id": UUID(child_view["tool_calls"][0]["id"])},
                )
        engine.dispose()


def test_interrupted_delegate_and_result_receipts_reconcile_from_saved_owners(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _chief_id, order_id = _order(client, "Interrupted team actions")
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        main = client.post(base, headers=ORIGIN).json()["session"]
        researcher = next(
            item
            for item in client.get(f"/api/projects/{project_id}/profiles").json()
            if item["name"] == "Researcher"
        )
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(main["id"])},
            )
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "team-runner")
        attempts = TaskSessionAttempts(gateway)
        chief = attempts.claim_next(UUID(project_id))
        assert chief is not None
        chief_tools = TaskSessionTools(gateway, UUID(project_id), UUID(main["id"]), chief["id"])
        delegate_input = {
            "profile_id": researcher["id"],
            "objective": "Review the evidence",
            "expected_result": "One supported finding",
            "acceptance_check": "Save a finding",
        }
        delegate_receipt = chief_tools._begin(
            chief_tools._scope(),
            "onefoundry.delegate_agent",
            "delegate-interrupted",
            hashlib.sha256(
                json.dumps(delegate_input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            allow_new=True,
        )
        delegated = chief_tools._execute(
            chief_tools._scope(), "onefoundry.delegate_agent", delegate_input, delegate_receipt["id"]
        )
        attempts.settle(UUID(project_id), chief["id"], "done", public_reply="Research assigned.")
        child = attempts.claim_next(UUID(project_id))
        assert child is not None and child["session_id"] == UUID(delegated["child_session_id"])
        child_tools = TaskSessionTools(gateway, UUID(project_id), child["session_id"], child["id"])
        result_input = {"body": "{a non-JSON source note from the product site"}
        result_receipt = child_tools._begin(
            child_tools._scope(),
            "onefoundry.report_result",
            "result-interrupted",
            hashlib.sha256(
                json.dumps(result_input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            allow_new=True,
        )
        reported = child_tools._execute(
            child_tools._scope(), "onefoundry.report_result", result_input, result_receipt["id"]
        )
        attempts.settle(UUID(project_id), child["id"], "done", public_reply="Finding saved.")
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        assert (
            client.post(
                f"{base}/sessions/{child['session_id']}/continue",
                json={"client_attempt_id": str(uuid4())},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        next_chief = attempts.claim_next(UUID(project_id))
        assert next_chief is not None and next_chief["session_id"] == UUID(main["id"])
        assert (
            TaskSessionTools(
                gateway, UUID(project_id), UUID(main["id"]), next_chief["id"]
            ).reconcile_previous_actions()
            == 1
        )
        attempts.settle(UUID(project_id), next_chief["id"], "done", public_reply="Research reviewed.")
        reconciled = client.post(f"{base}/sessions/{child['session_id']}/reconcile", headers=ORIGIN)
        assert reconciled.status_code == 200 and reconciled.json() == {"reconciled_actions": 1}
        assert attempts.claim_next(UUID(project_id)) is None
        assert client.post(f"{base}/sessions/{child['session_id']}/reconcile", headers=ORIGIN).json() == {
            "reconciled_actions": 0
        }
        with engine.connect() as connection:
            saved = {
                row["id"]: row["result"]
                for row in connection.execute(
                    text(
                        "SELECT id,result FROM task_session_tool_calls WHERE project_id=:p "
                        "AND id IN (:delegate,:result)"
                    ),
                    {
                        "p": UUID(project_id),
                        "delegate": delegate_receipt["id"],
                        "result": result_receipt["id"],
                    },
                ).mappings()
            }
        assert saved[delegate_receipt["id"]] == {
            **delegated,
            "status": "satisfied",  # Readback reflects the assignment's current saved state.
        }
        assert saved[result_receipt["id"]] == reported
        engine.dispose()


@pytest.mark.parametrize("byte_limit", [16384, None])
def test_long_team_chat_keeps_latest_confirmed_messages_and_records_omissions(
    tmp_path: Path, byte_limit: int | None
) -> None:
    settings = _settings(tmp_path).model_copy(update={"onefoundry_max_context_bytes": byte_limit})
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id = _order(client, "Long session context")
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        opened = client.post(base, headers=ORIGIN).json()
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                {"p": UUID(project_id), "s": UUID(opened["session"]["id"])},
            )
        for number in range(35):
            saved = client.post(
                f"{base}/messages",
                json={
                    "client_message_id": str(uuid4()),
                    "expected_sequence": number,
                    "body": f"Message {number:02d}: " + "plain project detail " * 60,
                },
                headers=ORIGIN,
            )
            assert saved.status_code == 202, saved.text
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(engine, "operator")
        claimed = TaskSessionAttempts(gateway).claim_next(UUID(project_id))
        assert claimed is not None and claimed["input_sequence"] == 35
        snapshot = TaskSessionContext(gateway, settings).prepare(UUID(project_id), claimed["id"])
        assert "Message 34" in snapshot.prompt
        if byte_limit is None:
            assert "Message 00" in snapshot.prompt
            assert snapshot.omitted_count == 0
            assert len(snapshot.prompt.encode()) > 16384
        else:
            assert "Message 00" not in snapshot.prompt
            assert snapshot.omitted_count > 0
            assert len(snapshot.prompt.encode()) <= byte_limit
        view = client.get(base).json()
        assert len(view["messages"]) == 35
        assert view["attempts"][0]["context_omitted_count"] == snapshot.omitted_count
        engine.dispose()


def _team_turn(client: TestClient, settings: Settings, name: str) -> tuple[str, str, str, ToolGateway]:
    project_id, _chief_id, order_id = _order(client, name)
    base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
    root = client.post(base, headers=ORIGIN).json()["session"]
    bootstrap_url = settings.database_url.set(
        username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
    )
    with create_engine(bootstrap_url).begin() as connection:
        connection.execute(
            text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
            {"p": UUID(project_id), "s": UUID(root["id"])},
        )
    queued = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
    assert queued.status_code == 202, queued.text
    gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
    return project_id, order_id, root["id"], gateway


def _delegate_args(profile_id: str, revision: int = 1) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "objective": "Collect product facts",
        "expected_result": "A short fact list",
        "acceptance_check": "Each fact has a source",
        "plan_revision": revision,
    }


def _reason(response: dict[str, object]) -> object:
    items = response["contentItems"]
    assert isinstance(items, list)
    return json.loads(items[0]["text"]).get("reason")


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("manual", [False, True])
def test_chief_synthesis_after_canceled_branch_in_same_revision(
    tmp_path: Path, replacement: bool, manual: bool
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, order_id, root_id, gateway = _team_turn(client, settings, "Canceled branch synthesis")
        project = UUID(project_id)
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        profiles = {
            row["name"]: row["id"] for row in client.get(f"/api/projects/{project_id}/profiles").json()
        }
        attempts = TaskSessionAttempts(gateway)
        decisions = TaskSessionDecisions(gateway)
        chief = attempts.claim_next(project)
        assert chief is not None
        tools = TaskSessionTools(gateway, project, UUID(root_id), chief["id"])
        for name in ("Researcher", "QA"):
            assert tools.invoke("onefoundry.delegate_agent", _delegate_args(profiles[name]), name)["success"]
        attempts.settle(project, chief["id"], "done", public_reply="Evidence assigned.")
        child = attempts.claim_next(project)
        assert child is not None
        TaskSessionAssignments(gateway).report_text_result(
            project, child["session_id"], child["id"], uuid4(), "Confirmed source finding."
        )
        attempts.settle(project, child["id"], "done", public_reply="Finding saved.")
        rejected = attempts.claim_next(project)
        assert rejected is not None
        attempts.settle(project, rejected["id"], "failed", reason="provider_turn_failed")
        assert decisions.dispatch_next(project) is not None
        replan = attempts.claim_next(project)
        assert replan is not None and replan["session_id"] == UUID(root_id)
        bind_team_run(gateway, project, replan)
        assignments = client.get(base).json()["assignments"]
        bad = next(row for row in assignments if row["child_session_id"] == str(rejected["session_id"]))
        tools = TaskSessionTools(gateway, project, UUID(root_id), replan["id"])
        assert tools.invoke("onefoundry.cancel_assignment", {"assignment_id": bad["id"]}, "cancel")["success"]
        if replacement:
            assert tools.invoke("onefoundry.delegate_agent", _delegate_args(profiles["QA"]), "replacement")[
                "success"
            ]
        attempts.settle(
            project, replan["id"], "done", public_reply="Canceled branch; remaining evidence retained."
        )
        if replacement:
            assert decisions.dispatch_next(project) is None  # The replacement is still unfinished.
            child = attempts.claim_next(project)
            assert child is not None
            TaskSessionAssignments(gateway).report_text_result(
                project, child["session_id"], child["id"], uuid4(), "Fresh independent evidence."
            )
            attempts.settle(project, child["id"], "done", public_reply="Replacement result saved.")
        view = client.get(base).json()
        assert all(row["status"] in ("satisfied", "canceled") for row in view["assignments"])
        synthesis = [row for row in view["decision_causes"] if row["kind"] == "synthesis"]
        assert len(synthesis) == 1 and synthesis[0]["dispatch_attempt_id"] is None
        assert decisions.dispatch_next(uuid4()) is None
        if manual:
            response = client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            )
            assert response.status_code == 202, response.text
        else:
            assert decisions.dispatch_next(project) is not None
        assert decisions.dispatch(project, UUID(order_id)) is None
        final = attempts.claim_next(project)
        assert final is not None and final["session_id"] == UUID(root_id)
        context = TaskSessionContext(gateway, settings).prepare(project, final["id"])
        assert "Chief decision cause (synthesis)" in context.prompt
        assert "Confirmed source finding." in context.prompt
        if replacement:
            assert "Fresh independent evidence." in context.prompt
        attempts.settle(project, final["id"], "done", public_reply="Evidence synthesized.")
        assert decisions.dispatch_next(project) is None
        gateway.engine.dispose()


@pytest.mark.parametrize("state", ["empty", "all_canceled", "unfinished", "blocked"])
def test_canceled_branches_do_not_supply_missing_evidence(tmp_path: Path, state: str) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, order_id, root_id, gateway = _team_turn(client, settings, "Incomplete evidence")
        project = UUID(project_id)
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        researcher = next(
            row
            for row in client.get(f"/api/projects/{project_id}/profiles").json()
            if row["name"] == "Researcher"
        )
        attempts = TaskSessionAttempts(gateway)
        decisions = TaskSessionDecisions(gateway)
        chief = attempts.claim_next(project)
        assert chief is not None
        if state == "empty":
            attempts.settle(project, chief["id"], "done", public_reply="No evidence assigned.")
            with gateway.engine.begin() as connection:
                decisions.record_synthesis_if_ready(connection, project, UUID(order_id), 1)
            assert client.get(base).json()["decision_causes"] == []
        else:
            tools = TaskSessionTools(gateway, project, UUID(root_id), chief["id"])
            assert tools.invoke("onefoundry.delegate_agent", _delegate_args(researcher["id"]), "first")[
                "success"
            ]
            attempts.settle(project, chief["id"], "done", public_reply="Evidence assigned.")
            child = attempts.claim_next(project)
            assert child is not None
            attempts.settle(project, child["id"], "failed", reason="provider_turn_failed")
            assert decisions.dispatch_next(project) is not None
            chief = attempts.claim_next(project)
            assert chief is not None and chief["session_id"] == UUID(root_id)
            bind_team_run(gateway, project, chief)
            assignment = client.get(base).json()["assignments"][0]
            tools = TaskSessionTools(gateway, project, UUID(root_id), chief["id"])
            assert tools.invoke(
                "onefoundry.cancel_assignment", {"assignment_id": assignment["id"]}, "cancel"
            )["success"]
            if state != "all_canceled":
                assert tools.invoke(
                    "onefoundry.delegate_agent", _delegate_args(researcher["id"]), "replacement"
                )["success"]
            attempts.settle(project, chief["id"], "done", public_reply="Branch canceled.")
            if state == "blocked":
                child = attempts.claim_next(project)
                assert child is not None
                attempts.settle(project, child["id"], "failed", reason="provider_turn_failed")
            assert not any(row["kind"] == "synthesis" for row in client.get(base).json()["decision_causes"])
        if state == "blocked":
            decision = decisions.dispatch_next(project)
            assert decision is not None
            chief = attempts.claim_next(project)
            assert chief is not None
            prompt = TaskSessionContext(gateway, settings).prepare(project, chief["id"]).prompt
            assert "Chief decision cause (blocker)" in prompt
            assert "Chief decision cause (synthesis)" not in prompt
        else:
            assert decisions.dispatch_next(project) is None
        gateway.engine.dispose()


def test_team_limits_stop_fan_out_depth_revisions_and_chief_loops(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "team_max_assignments_per_order": 3,
            "team_max_delegation_depth": 1,
            "team_max_plan_revisions": 1,
            "team_max_chief_decisions_per_order": 1,
        }
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, order_id, root_id, gateway = _team_turn(client, settings, "Team limits")
        project = UUID(project_id)
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        profiles = {
            row["name"]: row["id"] for row in client.get(f"/api/projects/{project_id}/profiles").json()
        }
        bootstrap_url = settings.database_url.set(
            username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
        )
        with create_engine(bootstrap_url).begin() as connection:
            connection.execute(
                text(
                    "UPDATE profiles SET allowed_tools=allowed_tools || '[\"delegate_agent_v1\"]'::jsonb "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"p": project, "id": UUID(profiles["Researcher"])},
            )
        attempts = TaskSessionAttempts(gateway)
        chief_turn = attempts.claim_next(project)
        assert chief_turn is not None
        chief_tools = TaskSessionTools(gateway, project, UUID(root_id), chief_turn["id"])
        revised = chief_tools.invoke(
            "onefoundry.delegate_agent", _delegate_args(profiles["Researcher"], revision=2), "too-late"
        )
        assert revised["success"] is False and _reason(revised) == "team_limit_plan_revisions"
        for call_id, name in (("one", "Researcher"), ("two", "GTM"), ("three", "QA")):
            saved = chief_tools.invoke("onefoundry.delegate_agent", _delegate_args(profiles[name]), call_id)
            assert saved["success"] is True
        fourth = chief_tools.invoke("onefoundry.delegate_agent", _delegate_args(profiles["Sales"]), "four")
        assert fourth["success"] is False and _reason(fourth) == "team_limit_assignments"
        view = client.get(base).json()
        assert len(view["assignments"]) == 3
        assert {row["reason"] for row in view["tool_calls"] if row["status"] == "failed"} == {
            "team_limit_plan_revisions",
            "team_limit_assignments",
        }
        attempts.settle(project, chief_turn["id"], "done", public_reply="Three assignments saved.")

        research_turn = attempts.claim_next(project)
        assert research_turn is not None
        research_session = research_turn["session_id"]
        research_tools = TaskSessionTools(gateway, project, research_session, research_turn["id"])
        nested = research_tools.invoke("onefoundry.delegate_agent", _delegate_args(profiles["QA"]), "nested")
        assert nested["success"] is False and _reason(nested) == "team_limit_delegation_depth"
        attempts.settle(project, research_turn["id"], "failed", reason="provider_turn_failed")
        for _ in range(2):
            child_turn = attempts.claim_next(project)
            assert child_turn is not None and child_turn["session_id"] != UUID(root_id)
            attempts.settle(project, child_turn["id"], "failed", reason="provider_turn_failed")
        decisions = TaskSessionDecisions(gateway)
        first_decision = decisions.dispatch_next(project)
        assert first_decision is not None and len(first_decision["cause_ids"]) == 3
        chief_again = attempts.claim_next(project)
        assert chief_again is not None and chief_again["id"] == first_decision["attempt_id"]
        attempts.settle(project, chief_again["id"], "done", public_reply="Retry the research only.")

        retried = client.post(
            f"{base}/sessions/{research_session}/continue",
            json={"client_attempt_id": str(uuid4())},
            headers=ORIGIN,
        )
        assert retried.status_code == 202, retried.text
        research_again = attempts.claim_next(project)
        assert research_again is not None and research_again["session_id"] == research_session
        attempts.settle(project, research_again["id"], "failed", reason="provider_turn_failed")
        assert decisions.dispatch_next(project) is None
        paused = client.get(base).json()
        assert paused["session"]["status"] == "waiting_for_operator"
        assert attempts.claim_next(project) is None
        assert decisions.dispatch_next(project) is None
        with gateway.engine.connect() as connection:
            limit_events = connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:p "
                    "AND event_type='task_session_limit_reached'"
                ),
                {"p": project},
            )
        assert limit_events == 1
        reply = client.post(
            f"{base}/messages",
            json={
                "client_message_id": str(uuid4()),
                "expected_sequence": paused["messages"][-1]["sequence"],
                "body": "Stop research; summarize what we have.",
            },
            headers=ORIGIN,
        )
        assert reply.status_code == 202, reply.text
        assert reply.json()["state"] == "queued"
        operator_turn = attempts.claim_next(project)
        assert operator_turn is not None and operator_turn["session_id"] == UUID(root_id)
        snapshot = TaskSessionContext(gateway, settings).prepare(project, operator_turn["id"])
        assert "Chief decision cause (blocker): Researcher" in snapshot.prompt
        gateway.engine.dispose()


def test_pre_send_failure_retries_once_after_delay_without_waking_chief(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"team_retry_delay_seconds": 5})
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, order_id, root_id, gateway = _team_turn(client, settings, "Team retry")
        project = UUID(project_id)
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        profiles = {
            row["name"]: row["id"] for row in client.get(f"/api/projects/{project_id}/profiles").json()
        }
        attempts = TaskSessionAttempts(gateway)
        chief_turn = attempts.claim_next(project)
        assert chief_turn is not None
        tools = TaskSessionTools(gateway, project, UUID(root_id), chief_turn["id"])
        saved = tools.invoke("onefoundry.delegate_agent", _delegate_args(profiles["Researcher"]), "assign")
        assert saved["success"] is True
        # A turn that already ran a tool is never retried automatically.
        acted = attempts.settle(
            project, chief_turn["id"], "failed", reason="provider_rate_limited", retryable=True
        )
        assert acted["next_attempt_id"] is None

        research_turn = attempts.claim_next(project)
        assert research_turn is not None
        first = attempts.settle(
            project, research_turn["id"], "failed", reason="provider_rate_limited", retryable=True
        )
        assert first["next_attempt_id"] is not None
        view = client.get(base).json()
        assert view["assignments"][0]["status"] == "active"
        assert view["decision_causes"] == []
        assert attempts.claim_next(project) is None
        time.sleep(5.5)
        retry_turn = attempts.claim_next(project)
        assert retry_turn is not None and retry_turn["id"] == first["next_attempt_id"]
        second = attempts.settle(
            project, retry_turn["id"], "failed", reason="provider_rate_limited", retryable=True
        )
        assert second["next_attempt_id"] is None
        view = client.get(base).json()
        assert view["assignments"][0]["status"] == "blocked"
        assert [row["kind"] for row in view["decision_causes"]] == ["blocker"]
        child = client.get(f"{base}/sessions/{research_turn['session_id']}").json()
        assert [row["retry_of_attempt_id"] for row in child["attempts"]] == [None, str(research_turn["id"])]
        with gateway.engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("UPDATE task_session_attempts SET not_before=NULL WHERE project_id=:p AND id=:a"),
                {"p": project, "a": retry_turn["id"]},
            )
        gateway.engine.dispose()


def test_sdk_worker_runs_each_agent_on_its_own_provider_and_switches_between_turns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, order_id, root_id, gateway = _team_turn(client, settings, "Provider-neutral team")
        project = UUID(project_id)
        base = f"/api/projects/{project_id}/work-orders/{order_id}/team-session"
        model_path = f"/api/projects/{project_id}/model"
        local = client.post(
            f"{model_path}/accounts",
            json={"provider": "ollama", "label": "Local", "endpoint": "http://ollama:11434"},
            headers=ORIGIN,
        ).json()["id"]
        hosted = client.post(
            f"{model_path}/accounts",
            json={"provider": "openrouter", "label": "Hosted", "api_key": "test-compatible-key"},
            headers=ORIGIN,
        ).json()["id"]
        for account, model in ((local, "qwen-test:latest"), (hosted, "vendor/chat-test")):
            with patch(
                "onefoundry.model_gateway.discover_models",
                return_value=[DiscoveredModel(model, model, "text")],
            ):
                refreshed = client.post(f"{model_path}/accounts/{account}/refresh", headers=ORIGIN)
            assert refreshed.status_code == 200, refreshed.text
        profiles = {
            row["name"]: row["id"] for row in client.get(f"/api/projects/{project_id}/profiles").json()
        }

        def bind(name: str, account: str, model: str) -> None:
            response = client.patch(
                f"/api/projects/{project_id}/profiles/{profiles[name]}",
                json={"model": model, "model_account_id": account, "allowed_tools": []},
                headers=ORIGIN,
            )
            assert response.status_code == 200, response.text

        bind("Chief of Staff", local, "qwen-test:latest")
        bind("Researcher", hosted, "vendor/chat-test")
        scripted: list[ScriptedModel] = [
            ScriptedModel(
                [
                    ModelStep(
                        output=[
                            function_call(
                                "delegate_agent",
                                _delegate_args(profiles["Researcher"]),
                                call_id="call_0",
                            )
                        ]
                    ),
                    ModelStep(output=[assistant_message("Researcher assigned.")]),
                ]
            ),
            ScriptedModel(
                [
                    ModelStep(
                        output=[
                            function_call("report_result", {"body": "Fact A [source 1]."}, call_id="call_0")
                        ]
                    ),
                    ModelStep(output=[assistant_message("Result reported.")]),
                ]
            ),
            ScriptedModel([ModelStep(output=[assistant_message("Summary: Fact A is verified.")])]),
            ScriptedModel([ModelStep.raise_error(_rate_limited())]),
        ]
        bound: list[tuple[str, str]] = []

        def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
            bound.append((runtime.provider, runtime.model_id))
            return TeamModel(scripted.pop(0), runtime.provider, runtime.model_id, False, False)

        worker = TaskSessionSdkTurn(gateway, settings, model_factory=factory)
        assert (worker.run_next(project) or {})["outcome"] == "done"
        view = client.get(base).json()
        assert [row["status"] for row in view["assignments"]] == ["ready"]
        assert view["messages"][-1]["body"] == "Researcher assigned."
        assert (worker.run_next(project) or {})["outcome"] == "done"
        view = client.get(base).json()
        assert [row["status"] for row in view["assignments"]] == ["satisfied"]
        assert [row["kind"] for row in view["decision_causes"]] == ["synthesis"]

        bind("Chief of Staff", hosted, "vendor/chat-test")
        assert TaskSessionDecisions(gateway).dispatch_next(project) is not None
        assert (worker.run_next(project) or {})["outcome"] == "done"
        view = client.get(base).json()
        assert view["messages"][-1]["body"] == "Summary: Fact A is verified."
        assert [(row["provider"], row["model_id"]) for row in view["attempts"]] == [
            ("ollama", "qwen-test:latest"),
            ("openrouter", "vendor/chat-test"),
        ]
        assert view["attempts"][-1]["context_dependency_ids"] == [view["assignments"][0]["id"]]
        assert bound == [
            ("ollama", "qwen-test:latest"),
            ("openrouter", "vendor/chat-test"),
            ("openrouter", "vendor/chat-test"),
        ]

        continued = client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        assert continued.status_code == 202, continued.text
        limited = worker.run_next(project)
        assert (
            limited is not None and limited["outcome"] == "failed" and limited["next_attempt_id"] is not None
        )
        view = client.get(base).json()
        assert view["attempts"][-2]["reason"] == "provider_rate_limited"
        assert view["attempts"][-1]["status"] == "queued"
        assert worker.run_next(project) is None
        assert scripted == []
        gateway.engine.dispose()


def _rate_limited() -> Exception:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)


@pytest.mark.parametrize("byte_limit", [8192, None])
def test_large_dependency_pages_preserve_declared_scope_and_allow_turn_preflight(
    tmp_path: Path, byte_limit: int | None
) -> None:
    settings = _settings(tmp_path).model_copy(update={"onefoundry_max_context_bytes": byte_limit})
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, order_id, root_id, gateway = _team_turn(client, settings, "Large dependency paging")
        project = UUID(project_id)
        profiles = {
            row["name"]: row["id"] for row in client.get(f"/api/projects/{project_id}/profiles").json()
        }
        attempts = TaskSessionAttempts(gateway)
        chief = attempts.claim_next(project)
        assert chief is not None
        tools = TaskSessionTools(gateway, project, UUID(root_id), chief["id"])
        assignments = TaskSessionAssignments(gateway)
        first = assignments.delegate(
            project,
            UUID(root_id),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["Researcher"]),
                objective="Read public facts",
                expected_result="Public evidence",
                acceptance_check="No private facts",
            ),
        )
        second = assignments.delegate(
            project,
            UUID(root_id),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["GTM"]),
                objective="Use the confirmed public facts",
                expected_result="A checked story",
                acceptance_check="Use the exact evidence",
                depends_on=(first["id"],),
            ),
        )
        unrelated = assignments.delegate(
            project,
            UUID(root_id),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["QA"]),
                objective="Unrelated task",
                expected_result="Unrelated finding",
                acceptance_check="Do not include in GTM",
            ),
        )
        # The same project has more profiles than one configured roster page.
        page = tools.invoke("onefoundry.list_agents", {"offset": 0}, "roster-page-0")
        assert page["success"]
        data = json.loads(page["contentItems"][0]["text"])
        assert len(data["agents"]) == settings.role_context_max_items
        assert data["next_offset"] is not None
        next_page = tools.invoke(
            "onefoundry.list_agents", {"offset": data["next_offset"]}, "roster-page-next"
        )
        assert next_page["success"]
        all_names = {row["name"] for row in data["agents"]} | {
            row["name"] for row in json.loads(next_page["contentItems"][0]["text"])["agents"]
        }
        assert all_names == set(profiles)
        attempts.settle(project, chief["id"], "done", public_reply="Assignments saved.")
        research = attempts.claim_next(project)
        assert research is not None and research["session_id"] == first["child_session_id"]
        full = json.dumps(
            {
                "summary": "Checked public evidence.",
                "artifact": {
                    "artifact_id": str(uuid4()),
                    "run_id": str(research["id"]),
                    "kind": "markdown",
                    "sha256": "b" * 64,
                    "content": "Public evidence café 中文. " * 600,
                },
            },
            ensure_ascii=False,
        )
        assignments.report_text_result(project, research["session_id"], research["id"], uuid4(), full)
        attempts.settle(project, research["id"], "done", public_reply="Public evidence saved.")
        active = attempts.claim_next(project)
        assert active is not None
        if active["session_id"] == unrelated["child_session_id"]:
            assignments.report_text_result(
                project, active["session_id"], active["id"], uuid4(), "Private to its declared edge"
            )
            attempts.settle(project, active["id"], "done", public_reply="Unrelated result saved.")
            active = attempts.claim_next(project)
        assert active is not None and active["session_id"] == second["child_session_id"]
        snapshot = TaskSessionContext(gateway, settings).prepare(project, active["id"])
        if byte_limit is None:
            assert full in snapshot.prompt
            assert len(snapshot.prompt.encode()) > 16384
        else:
            assert len(snapshot.prompt.encode()) <= byte_limit
            assert "read_dependency_result" in snapshot.prompt
            assert "context_complete" in snapshot.prompt
        assert snapshot.dependency_ids == (first["id"],)
        assert "Private to its declared edge" not in snapshot.prompt
        child_tools = TaskSessionTools(gateway, project, active["session_id"], active["id"])
        other_project_id, _other_order, other_root_id, other_gateway = _team_turn(
            client, settings, "Other project dependency"
        )
        other_project = UUID(other_project_id)
        other_chief = TaskSessionAttempts(other_gateway).claim_next(other_project)
        assert other_chief is not None
        other_researcher = next(
            row
            for row in client.get(f"/api/projects/{other_project_id}/profiles").json()
            if row["name"] == "Researcher"
        )
        other_assignment = TaskSessionAssignments(other_gateway).delegate(
            other_project,
            UUID(other_root_id),
            other_chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(other_researcher["id"]),
                objective="Other project only",
                expected_result="Scoped finding",
                acceptance_check="Never expose to the first project",
            ),
        )
        TaskSessionAttempts(other_gateway).settle(
            other_project, other_chief["id"], "done", public_reply="Other project assignment saved."
        )
        other_turn = TaskSessionAttempts(other_gateway).claim_next(other_project)
        assert other_turn is not None
        TaskSessionAssignments(other_gateway).report_text_result(
            other_project, other_turn["session_id"], other_turn["id"], uuid4(), "Other project result"
        )
        TaskSessionAttempts(other_gateway).settle(
            other_project, other_turn["id"], "done", public_reply="Other project result saved."
        )
        offset = 0
        reconstructed = []
        while True:
            response = child_tools.invoke(
                "onefoundry.read_dependency_result",
                {"assignment_id": str(first["id"]), "offset": offset},
                f"read-{offset}",
            )
            assert response["success"]
            assert (
                child_tools.invoke(
                    "onefoundry.read_dependency_result",
                    {"assignment_id": str(first["id"]), "offset": offset},
                    f"read-{offset}",
                )
                == response
            )
            data = json.loads(response["contentItems"][0]["text"])
            assert data["body_sha256"] == hashlib.sha256(full.encode()).hexdigest()
            reconstructed.append(data["content"])
            if data["next_offset"] is None:
                break
            offset = data["next_offset"]
        assert "".join(reconstructed) == full
        for forbidden in (unrelated["id"], other_assignment["id"], uuid4()):
            assert not child_tools.invoke(
                "onefoundry.read_dependency_result", {"assignment_id": str(forbidden)}, str(forbidden)
            )["success"]
        assert not child_tools.invoke(
            "onefoundry.read_dependency_result", {"assignment_id": str(first["id"]), "offset": -1}, "negative"
        )["success"]
        assert not child_tools.invoke(
            "onefoundry.read_dependency_result", {"assignment_id": "malformed"}, "invalid-id"
        )["success"]
        assert not child_tools.invoke(
            "onefoundry.read_dependency_result",
            {"assignment_id": str(first["id"]), "offset": len(full) + 1},
            "past-end",
        )["success"]
        with pytest.raises(Conflict, match="outside an active agent turn"):
            tools.invoke("onefoundry.list_agents", {}, "settled-turn")
        other_gateway.engine.dispose()
        gateway.engine.dispose()
