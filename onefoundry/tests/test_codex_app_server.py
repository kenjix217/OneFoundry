"""Offline JSONL protocol tests; the real Codex executable is never launched."""

import sys
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from runner.codex_app_server import (
    AppServerError,
    AppServerThreadNotFound,
    AppServerTimeout,
    CodexAppServer,
)


def fake_server(tmp_path: Path, body: str) -> list[str]:
    source = dedent(
        """
        import json
        import sys
        import time

        def receive():
            return json.loads(sys.stdin.readline())

        def send(value):
            print(json.dumps(value), flush=True)

        initialize = receive()
        assert initialize["method"] == "initialize"
        assert initialize["params"]["clientInfo"]["name"] == "onefoundry"
        send({"id": initialize["id"], "result": {"userAgent": "fake"}})
        assert receive() == {"method": "initialized"}
        """
    ) + dedent(body)
    script = tmp_path / "fake_app_server.py"
    script.write_text(source)
    return [sys.executable, str(script)]


def adapter(tmp_path: Path, body: str, **kwargs: Any) -> CodexAppServer:
    return CodexAppServer(
        fake_server(tmp_path, body),
        cwd=tmp_path,
        environment={"PYTHONUNBUFFERED": "1"},
        timeout_seconds=0.5,
        **kwargs,
    )


def test_handshake_account_thread_turn_stream_and_approval(tmp_path: Path) -> None:
    decisions: list[tuple[str, str]] = []

    def approve(method: str, params: dict[str, Any]) -> str:
        decisions.append((method, params["itemId"]))
        return "decline"

    body = """
    account = receive()
    assert account["method"] == "account/read"
    assert account["params"] == {"refreshToken": False}
    send({"id": account["id"], "result": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}})
    start = receive()
    assert start["method"] == "thread/start"
    assert start["params"]["sandbox"] == "read-only"
    assert start["params"]["approvalPolicy"] == "never"
    send({"method": "thread/started", "params": {"thread": {"id": "thread-1"}}})
    send({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})
    turn = receive()
    assert turn["method"] == "turn/start"
    assert turn["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["params"]["input"] == [{"type": "text", "text": "Inspect only"}]
    send({"id": turn["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
    send({"method": "item/agentMessage/delta", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "delta": "hello"}})
    send({"id": 99, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1", "command": "touch x"}})
    decision = receive()
    assert decision == {"id": 99, "result": {"decision": "decline"}}
    send({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
    """
    with adapter(tmp_path, body, approval_callback=approve) as client:
        assert client.account_ready()
        assert client.start_thread(repository=tmp_path, model="test-model") == "thread-1"
        assert client.start_turn("Inspect only", repository=tmp_path) == "turn-1"
        assert client.next_event(timeout_seconds=1)["method"] == "thread/started"
        assert client.next_event(timeout_seconds=1)["method"] == "item/agentMessage/delta"
        assert client.next_event(timeout_seconds=1)["params"]["turn"]["status"] == "completed"
        assert client.turn_id is None
    assert decisions == [("item/commandExecution/requestApproval", "item-1")]


def test_resume_interrupt_and_terminal_readback(tmp_path: Path) -> None:
    body = """
    resume = receive()
    assert resume["method"] == "thread/resume"
    assert resume["params"]["threadId"] == "saved-thread"
    assert resume["params"]["sandbox"] == "read-only"
    send({"id": resume["id"], "result": {"thread": {"id": "saved-thread"}}})
    turn = receive()
    send({"id": turn["id"], "result": {"turn": {"id": "turn-2", "status": "inProgress"}}})
    interrupt = receive()
    assert interrupt["method"] == "turn/interrupt"
    assert interrupt["params"] == {"threadId": "saved-thread", "turnId": "turn-2"}
    send({"id": interrupt["id"], "result": {}})
    send({"method": "turn/completed", "params": {"turn": {"id": "turn-2", "status": "interrupted"}}})
    """
    with adapter(tmp_path, body) as client:
        assert client.resume_thread("saved-thread", repository=tmp_path) == "saved-thread"
        assert client.start_turn("Stop soon", repository=tmp_path) == "turn-2"
        client.interrupt()
        assert client.next_event(timeout_seconds=1)["params"]["turn"]["status"] == "interrupted"
        assert client.turn_id is None


def test_missing_unstarted_thread_has_typed_recovery_reason(tmp_path: Path) -> None:
    body = """
    resume = receive()
    assert resume["method"] == "thread/resume"
    send({"id": resume["id"], "error": {"code": -32600,
        "message": "no rollout found for thread id saved-thread"}})
    """
    with adapter(tmp_path, body) as client:
        with pytest.raises(AppServerThreadNotFound):
            client.resume_thread("saved-thread", repository=tmp_path)


def test_unknown_server_request_fails_closed(tmp_path: Path) -> None:
    body = """
    account = receive()
    send({"id": 77, "method": "dangerous/tool", "params": {}})
    denial = receive()
    assert denial["id"] == 77 and denial["error"]["code"] == -32601
    send({"id": account["id"], "result": {"account": None}})
    """
    with adapter(tmp_path, body) as client:
        assert client.account_ready() is False


def test_timeout_and_oversized_frame_fail_closed(tmp_path: Path) -> None:
    with adapter(tmp_path, "account = receive()\ntime.sleep(2)\n") as client:
        with pytest.raises(AppServerTimeout):
            client.account_ready()
    body = """
    account = receive()
    sys.stdout.write("x" * 65537)
    sys.stdout.flush()
    time.sleep(2)
    """
    with adapter(tmp_path, body) as client:
        with pytest.raises(AppServerError, match="message too large"):
            client.account_ready()


def test_invalid_approval_scope_does_not_call_callback(tmp_path: Path) -> None:
    body = """
    start = receive()
    send({"id": start["id"], "result": {"thread": {"id": "thread-1"}}})
    turn = receive()
    send({"id": turn["id"], "result": {"turn": {"id": "turn-1"}}})
    send({"id": 44, "method": "item/fileChange/requestApproval", "params": {
        "threadId": "other", "turnId": "turn-1", "itemId": "item-1"}})
    time.sleep(2)
    """
    with adapter(tmp_path, body, approval_callback=lambda _method, _params: pytest.fail("called")) as client:
        client.start_thread(repository=tmp_path, model="test-model")
        client.start_turn("Read", repository=tmp_path)
        with pytest.raises(AppServerError, match="scope mismatch"):
            client.next_event(timeout_seconds=1)


def test_scoped_dynamic_tool_call_is_deduplicated(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any], str]] = []
    tool = {
        "type": "namespace",
        "name": "onefoundry",
        "description": "Admitted work-order actions",
        "tools": [
            {
                "type": "function",
                "name": "read_task",
                "description": "Read the assigned task",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
    }

    def action(name: str, arguments: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        calls.append((name, dict(arguments), call_id))
        return {"success": True, "contentItems": [{"type": "inputText", "text": "saved result"}]}

    body = """
    assert initialize["params"]["capabilities"] == {"experimentalApi": True}
    start = receive()
    assert start["method"] == "thread/start"
    assert start["params"]["dynamicTools"][0]["name"] == "onefoundry"
    send({"id": start["id"], "result": {"thread": {"id": "team-thread"}}})
    turn = receive()
    send({"id": turn["id"], "result": {"turn": {"id": "team-turn"}}})
    for request_id in (80, 81):
        send({"id": request_id, "method": "item/tool/call", "params": {
            "threadId": "team-thread", "turnId": "team-turn", "callId": "call-1",
            "namespace": "onefoundry", "tool": "read_task", "arguments": {}}})
        reply = receive()
        assert reply == {"id": request_id, "result": {
            "success": True, "contentItems": [{"type": "inputText", "text": "saved result"}]}}
    send({"method": "turn/completed", "params": {"turn": {"id": "team-turn", "status": "completed"}}})
    """
    with adapter(tmp_path, body, dynamic_tool_callback=action) as client:
        assert (
            client.start_thread(repository=tmp_path, model="test-model", dynamic_tools=[tool])
            == "team-thread"
        )
        assert client.start_turn("Read assigned task", repository=tmp_path) == "team-turn"
        assert client.next_event(timeout_seconds=1)["method"] == "turn/completed"
    assert calls == [("onefoundry.read_task", {}, "call-1")]


def test_dynamic_tool_outside_admitted_set_fails_closed(tmp_path: Path) -> None:
    body = """
    start = receive()
    send({"id": start["id"], "result": {"thread": {"id": "team-thread"}}})
    turn = receive()
    send({"id": turn["id"], "result": {"turn": {"id": "team-turn"}}})
    send({"id": 80, "method": "item/tool/call", "params": {
        "threadId": "team-thread", "turnId": "team-turn", "callId": "call-2",
        "namespace": "onefoundry", "tool": "send_email", "arguments": {}}})
    """
    with adapter(
        tmp_path,
        body,
        dynamic_tool_callback=lambda *_args: pytest.fail("unadmitted tool executed"),
    ) as client:
        client.start_thread(
            repository=tmp_path,
            model="test-model",
            dynamic_tools=[
                {
                    "type": "namespace",
                    "name": "onefoundry",
                    "description": "Admitted work-order actions",
                    "tools": [
                        {
                            "type": "function",
                            "name": "read_task",
                            "description": "Read the assigned task",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ],
        )
        client.start_turn("Read", repository=tmp_path)
        with pytest.raises(AppServerError, match="not admitted"):
            client.next_event(timeout_seconds=1)
