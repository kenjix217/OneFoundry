"""The mounted operator inbox binds a PR comment without real GitHub egress."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.github_pr_comment_contract import SavedSource
from onefoundry.github_pr_comment_transport import GitHubCommentTransport
from onefoundry.tool_gateway import ToolGateway
from tests.test_github_pr_comment_proposal_postgres import HEAD, _setup

pytest_plugins = ("tests.test_github_pr_comment_proposal_postgres",)


def _proposal(client: TestClient, gateway: ToolGateway) -> tuple[str, str]:
    project_id, run_id, _, source = _setup(client, gateway)
    response = client.post(
        f"/api/projects/{project_id}/github-pr-comments",
        json={
            "run_id": str(run_id),
            "source_id": str(source.source_id),
            "number": 7,
            "comment": "Please check the error path.",
            "idempotency_key": str(uuid4()),
        },
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 201, response.text
    return str(project_id), response.json()["approval_id"]


def test_operator_approval_and_one_dispatch_use_fake_github_only(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def head(self: GitHubCommentTransport, credential: str, source: SavedSource, number: int) -> str:
        calls.append("head")
        return HEAD

    def post(
        self: GitHubCommentTransport, credential: str, source: SavedSource, number: int, body: str
    ) -> int:
        calls.append("post")
        assert number == 7 and body == "Please check the error path."
        return 12345

    def read(
        self: GitHubCommentTransport, credential: str, source: SavedSource, number: int, comment_id: int
    ) -> str:
        calls.append("read")
        assert number == 7 and comment_id == 12345
        return "Please check the error path."

    monkeypatch.setattr(GitHubCommentTransport, "current_head", head)
    monkeypatch.setattr(GitHubCommentTransport, "post_comment", post)
    monkeypatch.setattr(GitHubCommentTransport, "read_comment", read)
    project_id, approval_id = _proposal(client, gateway)
    approvals = client.get(f"/api/projects/{project_id}/approvals?status=pending")
    assert approvals.status_code == 200, approvals.text
    matching = next(item for item in approvals.json() if item["id"] == approval_id)
    assert matching["github_comment"] is True
    assert "commits may change before posting" in matching["target"]
    decision = client.post(
        f"/api/projects/{project_id}/approvals/{approval_id}/decision",
        json={"decision": "approved"},
        headers={"origin": "http://localhost:8765"},
    )
    assert decision.status_code == 200, decision.text
    before = client.get(f"/api/projects/{project_id}/github-pr-comments/{approval_id}")
    assert before.status_code == 200 and before.json()["status"] == "not_attempted"
    sent = client.post(
        f"/api/projects/{project_id}/github-pr-comments/{approval_id}/dispatch",
        headers={"origin": "http://localhost:8765"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "done"
    assert sent.json()["comment_id"] == 12345
    saved = client.get(f"/api/projects/{project_id}/github-pr-comments/{approval_id}")
    assert saved.json() == sent.json()
    repeated = client.post(
        f"/api/projects/{project_id}/github-pr-comments/{approval_id}/dispatch",
        headers={"origin": "http://localhost:8765"},
    )
    assert repeated.status_code == 409
    assert calls.count("post") == 1


def test_approval_rejects_removed_saved_source(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GitHubCommentTransport, "current_head", lambda *_: HEAD)
    project_id, approval_id = _proposal(client, gateway)
    with gateway.engine.connect() as connection:
        source_id = connection.scalar(
            text("SELECT id FROM project_sources WHERE project_id=:id AND kind='github' LIMIT 1"),
            {"id": project_id},
        )
    removed = client.delete(
        f"/api/projects/{project_id}/sources/{source_id}",
        headers={"origin": "http://localhost:8765"},
    )
    assert removed.status_code == 204, removed.text
    decision = client.post(
        f"/api/projects/{project_id}/approvals/{approval_id}/decision",
        json={"decision": "approved"},
        headers={"origin": "http://localhost:8765"},
    )
    assert decision.status_code == 409


def test_invalid_run_cannot_trigger_github_observation(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, _, _, source = _setup(client, gateway)
    observed: list[int] = []

    def head(self: GitHubCommentTransport, credential: str, saved: SavedSource, number: int) -> str:
        observed.append(number)
        return HEAD

    monkeypatch.setattr(GitHubCommentTransport, "current_head", head)
    response = client.post(
        f"/api/projects/{project_id}/github-pr-comments",
        json={
            "run_id": str(uuid4()),
            "source_id": str(source.source_id),
            "number": 7,
            "comment": "Please check this path.",
            "idempotency_key": str(uuid4()),
        },
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 404
    assert observed == []
