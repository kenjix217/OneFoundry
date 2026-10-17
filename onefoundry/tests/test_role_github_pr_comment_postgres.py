"""Role PR comment calls create inbox proposals and never dispatch comments."""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from onefoundry.role_github_pr_comment import parse_arguments
from onefoundry.role_tool_admission import ToolDenied, ToolDependencyFailure, ToolInputError
from onefoundry.tool_gateway import ToolGateway
from tests.test_github_pr_comment_proposal_postgres import HEAD, _approval, _setup
from tests.test_role_tool_admission_postgres import _tool_events

pytest_plugins = ("tests.test_github_pr_comment_proposal_postgres",)


def _arguments(source_id: object, *, comment: str = "Please check this path.") -> dict[str, object]:
    return {
        "source_id": str(source_id),
        "number": 7,
        "comment": comment,
        "idempotency_key": str(uuid4()),
    }


def test_role_comment_proposes_only_with_hash_only_receipt(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, profile_id, source = _setup(client, gateway)
    arguments = _arguments(source.source_id)
    with (
        patch("onefoundry.role_github_pr_comment._credential", return_value="fixture-credential"),
        patch("onefoundry.role_github_pr_comment.GitHubCommentTransport") as transport,
    ):
        transport.return_value.current_head.return_value = HEAD
        receipt = gateway.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            "comment-call",
            "github_pr_comment_v1",
            arguments,
            tool_version=1,
        )
        transport.return_value.current_head.assert_called_once_with("fixture-credential", source, 7)
        transport.return_value.post_comment.assert_not_called()
    assert receipt["result"]["status"] == "pending"
    approval = _approval(gateway, project_id, receipt["result"]["approval_id"])
    assert approval["actor_id"] == f"profile:{profile_id}"
    assert json.loads(str(approval["content"]))["comment"] == arguments["comment"]
    events = _tool_events(gateway, project_id, run_id)
    assert [item["event_type"] for item in events] == ["role_tool_started", "role_tool_finished"]
    assert str(arguments["comment"]) not in json.dumps(events)
    assert "fixture-credential" not in json.dumps(events)
    with pytest.raises(ToolDenied, match="already attempted"):
        gateway.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            "comment-call",
            "github_pr_comment_v1",
            arguments,
            tool_version=1,
        )


def test_role_comment_rejects_invalid_and_revoked_calls(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, profile_id, source = _setup(client, gateway)
    assert [tool["id"] for tool in gateway.available_role_tools(project_id, run_id, profile_id)] == [
        "github_pr_comment_v1"
    ]
    with pytest.raises(ToolInputError):
        parse_arguments(_arguments(source.source_id, comment="token=" + (("A1" * 12)[:24]) + ""))
    with patch("onefoundry.role_github_pr_comment._credential") as credential:
        with pytest.raises(ToolDenied):
            gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "invalid-call",
                "github_pr_comment_v1",
                {**_arguments(source.source_id), "number": True},
                tool_version=1,
            )
        credential.assert_not_called()
    with gateway.engine.begin() as connection:
        from sqlalchemy import text

        connection.execute(
            text("UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:project_id AND id=:id"),
            {"project_id": project_id, "id": profile_id},
        )
    assert gateway.available_role_tools(project_id, run_id, profile_id) == []
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            "revoked-call",
            "github_pr_comment_v1",
            _arguments(source.source_id),
            tool_version=1,
        )


def test_role_comment_started_call_does_not_retry_get(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, profile_id, source = _setup(client, gateway)
    arguments = _arguments(source.source_id)
    with patch("onefoundry.role_github_pr_comment._credential", return_value="fixture-credential"):
        with patch("onefoundry.role_github_pr_comment.GitHubCommentTransport") as transport:
            transport.return_value.current_head.side_effect = RuntimeError("read failed")
            with pytest.raises(ToolDependencyFailure):
                gateway.invoke_role_tool(
                    project_id,
                    run_id,
                    profile_id,
                    "unknown-call",
                    "github_pr_comment_v1",
                    arguments,
                    tool_version=1,
                )
            with pytest.raises(ToolDenied, match="already attempted"):
                gateway.invoke_role_tool(
                    project_id,
                    run_id,
                    profile_id,
                    "unknown-call",
                    "github_pr_comment_v1",
                    arguments,
                    tool_version=1,
                )
            transport.return_value.current_head.assert_called_once()
