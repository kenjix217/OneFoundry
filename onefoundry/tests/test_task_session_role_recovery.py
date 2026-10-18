"""Independent oracles for live repository-task failures observed after bootstrap."""

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest

from onefoundry.role_tool_admission import (
    ToolDependencyFailure,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    ToolPolicyRejected,
    ToolRepositoryReadFailure,
    ToolTimedOut,
)
from onefoundry.role_web_admission import WebDependencyFailure, WebPolicyFailure
from onefoundry.task_session_tools import ROLE, TaskSessionTools
from onefoundry.tool_gateway import ToolGateway


def bare_tools() -> TaskSessionTools:
    tools = object.__new__(TaskSessionTools)
    tools.project_id = uuid4()
    tools.session_id = uuid4()
    tools.attempt_id = uuid4()
    return tools


def test_native_dispatch_uses_advertised_registered_tool_version() -> None:
    tools = bare_tools()
    gateway = SimpleNamespace(invoke_role_tool=Mock(return_value={"status": "done"}))
    tools.gateway = cast(ToolGateway, gateway)
    profile = uuid4()
    tools._execute(
        {"profile_id": profile, "role_tools": [{"id": "approved_memory_v2", "version": 2}]},
        ROLE,
        {"tool_id": "approved_memory_v2", "arguments": {"query": "architecture"}},
        uuid4(),
    )
    assert gateway.invoke_role_tool.call_args.kwargs["tool_version"] == 2


@pytest.mark.parametrize(
    "error,reason",
    [
        (ToolDependencyFailure, "tool_dependency_failure"),
        (ToolOutputRejected, "tool_output_rejected"),
        (ToolPolicyRejected, "tool_policy_rejected"),
        (ToolTimedOut, "tool_timeout"),
    ],
)
def test_known_read_failure_is_saved_and_returned_for_recovery(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception], reason: str
) -> None:
    tools = bare_tools()
    scope = {"role_tools": [{"id": "repository_file_v1", "version": 1}]}
    receipt = uuid4()
    monkeypatch.setattr(tools, "_scope", Mock(return_value=scope))
    monkeypatch.setattr(tools, "_admitted", Mock(return_value={ROLE}))
    monkeypatch.setattr(tools, "_begin", Mock(return_value={"id": receipt, "status": "started"}))
    finish = Mock()
    monkeypatch.setattr(tools, "_finish", finish)
    monkeypatch.setattr(tools, "_execute", Mock(side_effect=error("read owner failed")))
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
    assert body["reason"] == reason
    assert finish.call_args.kwargs["reason"] == reason
    assert finish.call_args.kwargs["result"] is None


def test_repository_failure_replay_retains_safe_owner_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = bare_tools()
    arguments = {
        "tool_id": "repository_file_v1",
        "arguments": {"source_id": str(uuid4()), "path": "README.md"},
    }
    scope = {"role_tools": [{"id": "repository_file_v1", "version": 1}]}
    receipt = uuid4()
    monkeypatch.setattr(tools, "_scope", Mock(return_value=scope))
    monkeypatch.setattr(tools, "_admitted", Mock(return_value={ROLE}))
    monkeypatch.setattr(
        tools,
        "_begin",
        Mock(
            side_effect=[
                {"id": receipt, "status": "started"},
                {"id": receipt, "status": "failed", "reason": "source_content_protected"},
            ]
        ),
    )
    finish = Mock()
    monkeypatch.setattr(tools, "_finish", finish)
    execute = Mock(side_effect=ToolRepositoryReadFailure("source_content_protected", "repository_file_v1"))
    monkeypatch.setattr(tools, "_execute", execute)
    first = json.loads(tools.invoke(ROLE, arguments, "same-read")["contentItems"][0]["text"])
    replay = json.loads(tools.invoke(ROLE, arguments, "same-read")["contentItems"][0]["text"])
    assert first == replay
    assert first["reason"] == "source_content_protected"
    assert "secret screening" in first["next_step"]
    execute.assert_called_once()
    finish.assert_called_once()
    assert isinstance(
        ToolRepositoryReadFailure("source_unavailable", "repository_file_v1"), ToolDependencyFailure
    )


@pytest.mark.parametrize(
    "failure,code,reason",
    [
        (WebPolicyFailure, "web_response_too_large", "tool_policy_rejected"),
        (WebDependencyFailure, "web_http_status_unavailable", "tool_dependency_failure"),
    ],
)
def test_web_failure_replay_retains_content_free_owner_code(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[WebPolicyFailure] | type[WebDependencyFailure],
    code: str,
    reason: str,
) -> None:
    tools = bare_tools()
    arguments = {"tool_id": "public_page_fetch_v1", "arguments": {"url": "https://example.org/private"}}
    scope = {"role_tools": [{"id": "public_page_fetch_v1", "version": 1}]}
    receipt = uuid4()
    monkeypatch.setattr(tools, "_scope", Mock(return_value=scope))
    monkeypatch.setattr(tools, "_admitted", Mock(return_value={ROLE}))
    monkeypatch.setattr(
        tools,
        "_begin",
        Mock(
            side_effect=[
                {"id": receipt, "status": "started"},
                {"id": receipt, "status": "failed", "reason": code},
            ]
        ),
    )
    finish = Mock()
    monkeypatch.setattr(tools, "_finish", finish)
    execute = Mock(side_effect=failure(code))
    monkeypatch.setattr(tools, "_execute", execute)
    first = json.loads(tools.invoke(ROLE, arguments, "same-web-read")["contentItems"][0]["text"])
    replay = json.loads(tools.invoke(ROLE, arguments, "same-web-read")["contentItems"][0]["text"])
    assert first == replay
    assert first["reason"] == reason
    assert first["owner_reason"] == code
    assert "example.org" not in json.dumps(first)
    assert finish.call_args.kwargs["reason"] == code
    execute.assert_called_once()


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


@pytest.mark.parametrize(
    "reason", ["source_not_searched", "invalid_arguments", "not_admitted", "version_mismatch"]
)
def test_page_fetch_denial_explains_the_owner_correction(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from onefoundry.role_tool_admission import ToolDenied

    tools = bare_tools()
    scope = {"role_tools": [{"id": "public_page_fetch_v1", "version": 1}]}
    receipt = uuid4()
    monkeypatch.setattr(tools, "_scope", Mock(return_value=scope))
    monkeypatch.setattr(tools, "_admitted", Mock(return_value={ROLE}))
    monkeypatch.setattr(tools, "_begin", Mock(return_value={"id": receipt, "status": "started"}))
    monkeypatch.setattr(tools, "_finish", Mock())
    failure = ToolDenied("web tool call was denied")
    failure.reason = reason
    monkeypatch.setattr(tools, "_execute", Mock(side_effect=failure))
    response = tools.invoke(ROLE, {"tool_id": "public_page_fetch_v1", "arguments": {}}, "denied-fetch")
    body = json.loads(response["contentItems"][0]["text"])
    assert body["owner_reason"] == reason
    assert body["next_step"]
    if reason == "source_not_searched":
        assert "same run" in body["next_step"] and "public_web_search_v1" in body["next_step"]
    if reason == "invalid_arguments":
        assert "without URLs, domains or email addresses" in body["next_step"]
