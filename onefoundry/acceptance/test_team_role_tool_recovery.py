"""Independent oracles for live repository-task failures observed after bootstrap."""

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from onefoundry.role_tool_admission import ToolDependencyFailure, ToolOutcomeUnknown
from onefoundry.task_session_tools import ROLE, TaskSessionTools


def bare_tools() -> TaskSessionTools:
    tools = object.__new__(TaskSessionTools)
    tools.project_id = uuid4()
    tools.session_id = uuid4()
    tools.attempt_id = uuid4()
    return tools


def test_native_dispatch_uses_advertised_registered_tool_version() -> None:
    tools = bare_tools()
    gateway = SimpleNamespace(invoke_role_tool=Mock(return_value={"status": "done"}))
    tools.gateway = gateway
    profile = uuid4()
    tools._execute(
        {"profile_id": profile, "role_tools": [{"id": "approved_memory_v2", "version": 2}]},
        ROLE,
        {"tool_id": "approved_memory_v2", "arguments": {"query": "architecture"}},
        uuid4(),
    )
    assert gateway.invoke_role_tool.call_args.kwargs["tool_version"] == 2


def test_known_read_failure_is_saved_and_returned_for_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = bare_tools()
    scope = {"role_tools": [{"id": "repository_file_v1", "version": 1}]}
    receipt = uuid4()
    monkeypatch.setattr(tools, "_scope", Mock(return_value=scope))
    monkeypatch.setattr(tools, "_admitted", Mock(return_value={ROLE}))
    monkeypatch.setattr(tools, "_begin", Mock(return_value={"id": receipt, "status": "started"}))
    finish = Mock()
    monkeypatch.setattr(tools, "_finish", finish)
    monkeypatch.setattr(tools, "_execute", Mock(side_effect=ToolDependencyFailure("read owner failed")))
    result = tools.invoke(
        ROLE,
        {
            "tool_id": "repository_file_v1",
            "arguments": {"source_id": str(uuid4()), "path": "ARCHITECTURE.md"},
        },
        "known-read-failure",
    )
    assert result["success"] is False
    body = json.loads(result["contentItems"][0]["text"])
    assert body["reason"] == "tool_dependency_failure"
    assert finish.call_args.kwargs["reason"] == "tool_dependency_failure"
    assert finish.call_args.kwargs["result"] is None


def test_uncertain_effect_still_stops_without_claiming_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = bare_tools()
    scope = {"role_tools": [{"id": "repository_file_v1", "version": 1}]}
    monkeypatch.setattr(tools, "_scope", Mock(return_value=scope))
    monkeypatch.setattr(tools, "_admitted", Mock(return_value={ROLE}))
    monkeypatch.setattr(tools, "_begin", Mock(return_value={"id": uuid4(), "status": "started"}))
    finish = Mock()
    monkeypatch.setattr(tools, "_finish", finish)
    monkeypatch.setattr(tools, "_execute", Mock(side_effect=ToolOutcomeUnknown("receipt write unknown")))
    with pytest.raises(ToolOutcomeUnknown):
        tools.invoke(ROLE, {"tool_id": "repository_file_v1", "arguments": {}}, "unknown-read")
    finish.assert_not_called()
