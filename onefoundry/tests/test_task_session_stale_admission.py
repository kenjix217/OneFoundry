"""A saved Chief action can invalidate a tool advertised at turn start."""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from onefoundry.gateway_errors import Conflict
from onefoundry.role_tool_admission import ToolOutcomeUnknown
from onefoundry.task_session_tools import COMPLETE, DELEGATE, RETRY, TaskSessionTools
from onefoundry.tool_gateway import ToolGateway


def _tools() -> tuple[TaskSessionTools, dict[str, Any], dict[str, Any], Mock]:
    tools = object.__new__(TaskSessionTools)
    tools.project_id, tools.session_id, tools.attempt_id = (uuid4() for _ in range(3))
    scope: dict[str, Any] = {
        "kind": "main",
        "is_project_chief": True,
        "actionable": True,
        "allowed_tools": [],
        "role_tools": [],
        "assignment": None,
        "has_blocked": True,
        "can_complete": False,
        "profile_id": uuid4(),
        "root_work_order_id": uuid4(),
        "work_order_id": uuid4(),
    }
    tools._scope = lambda: scope  # type: ignore[method-assign]
    state: dict[str, Any] = {"active": True, "receipts": {}, "inserts": 0}
    connection = MagicMock()

    def scalar(sql: Any, _params: dict[str, Any]) -> Any:
        if "FROM projects" in str(sql):
            return "active"
        return tools.attempt_id if state["active"] else None

    def execute(sql: Any, params: dict[str, Any]) -> MagicMock:
        result = MagicMock()
        if "SELECT * FROM task_session_tool_calls" in str(sql):
            result.mappings.return_value.one_or_none.return_value = state["receipts"].get(params["call"])
        elif "INSERT INTO task_session_tool_calls" in str(sql):
            state["inserts"] += 1
            row = {
                "id": params["id"],
                "session_id": tools.session_id,
                "tool_name": params["name"],
                "arguments_sha256": params["digest"],
                "status": params["status"],
                "reason": params["reason"],
            }
            state["receipts"][params["call"]] = row
            result.mappings.return_value.one.return_value = row
        return result

    connection.scalar.side_effect = scalar
    connection.execute.side_effect = execute
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = connection
    event = Mock()
    tools.gateway = cast(ToolGateway, SimpleNamespace(engine=engine, _event=event))
    return tools, scope, state, event


def test_second_advertised_retry_after_last_blocker_clears_is_a_known_rejection() -> None:
    tools, scope, state, event = _tools()
    assert "retry_assignment" in {item["name"] for item in tools.specifications()[0]["tools"]}

    def first_retry(_scope: dict[str, Any], _name: str, _args: dict[str, Any], _id: Any) -> dict[str, str]:
        scope["has_blocked"] = False
        return {"status": "queued"}

    execute = Mock(side_effect=first_retry)
    tools._execute = execute  # type: ignore[method-assign]
    tools._finish = Mock()  # type: ignore[method-assign]
    args = {"assignment_id": str(uuid4()), "direction": "Use the saved evidence."}
    assert tools.invoke(RETRY, args, "first-retry")["success"] is True
    assert RETRY not in tools._admitted(scope)

    response = tools.invoke(RETRY, args, "second-advertised-retry")
    assert response["success"] is False
    assert json.loads(response["contentItems"][0]["text"])["reason"] == "tool_no_longer_admitted"
    assert state["receipts"]["second-advertised-retry"]["status"] == "failed"
    assert state["inserts"] == 2
    assert [call.args[4] for call in event.call_args_list[-2:]] == [
        "task_session_tool_started",
        "task_session_tool_finished",
    ]
    execute.assert_called_once()

    replay = tools.invoke(RETRY, args, "second-advertised-retry")
    assert replay == response
    assert state["inserts"] == 2
    execute.assert_called_once()

    with pytest.raises(Conflict, match="provider tool call identity changed"):
        tools.invoke(RETRY, {**args, "direction": "Changed direction."}, "second-advertised-retry")
    assert state["inserts"] == 2


def test_inactive_turn_and_unknown_tool_remain_fatal_before_dispatch() -> None:
    tools, scope, state, _event = _tools()
    scope["has_blocked"] = False
    execute = Mock()
    tools._execute = execute  # type: ignore[method-assign]
    state["active"] = False
    with pytest.raises(Conflict, match="outside an active agent turn"):
        tools.invoke(RETRY, {}, "inactive-retry")
    assert state["inserts"] == 0
    state["active"] = True
    with pytest.raises(Conflict, match="team tool is not admitted"):
        tools.invoke("onefoundry.unknown_tool", {}, "unknown-tool")
    assert state["inserts"] == 0
    execute.assert_not_called()


def test_advertised_completion_after_new_delegation_is_a_known_rejection() -> None:
    tools, scope, state, _event = _tools()
    scope["can_complete"] = True
    assert "submit_completion" in {item["name"] for item in tools.specifications()[0]["tools"]}

    def delegate(_scope: dict[str, Any], _name: str, _args: dict[str, Any], _id: Any) -> dict[str, str]:
        scope["can_complete"] = False
        return {"status": "ready"}

    execute = Mock(side_effect=delegate)
    tools._execute = execute  # type: ignore[method-assign]
    tools._finish = Mock()  # type: ignore[method-assign]
    assert tools.invoke(DELEGATE, {}, "new-delegation")["success"] is True
    assert COMPLETE not in tools._admitted(scope)

    response = tools.invoke(COMPLETE, {}, "stale-completion")
    assert response["success"] is False
    assert json.loads(response["contentItems"][0]["text"])["reason"] == "tool_no_longer_admitted"
    assert state["receipts"]["stale-completion"]["status"] == "failed"
    execute.assert_called_once()


def test_post_dispatch_unknown_still_propagates_without_failed_receipt() -> None:
    tools, _scope, state, _event = _tools()
    tools._execute = Mock(side_effect=ToolOutcomeUnknown("effect uncertain"))  # type: ignore[method-assign]
    finish = Mock()
    tools._finish = finish  # type: ignore[method-assign]
    with pytest.raises(ToolOutcomeUnknown):
        tools.invoke(RETRY, {}, "uncertain-effect")
    assert state["receipts"]["uncertain-effect"]["status"] == "started"
    finish.assert_not_called()
