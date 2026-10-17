"""Approved PR comments use one durable attempt and offline transport observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError

from onefoundry import github_pr_comment_dispatch as comment_dispatch
from onefoundry.gateway_errors import Conflict
from onefoundry.github_pr_comment_contract import CommentProposal
from onefoundry.github_pr_comment_dispatch import dispatch, status
from onefoundry.github_pr_comment_proposal import ProposalInput, propose
from onefoundry.tool_gateway import ToolGateway
from tests.test_github_pr_comment_proposal_postgres import (
    HEAD,
    _observation,
    _setup,
)

pytest_plugins = ["tests.test_github_pr_comment_proposal_postgres"]


class OfflineTransport:
    def __init__(
        self,
        *,
        head: str = HEAD,
        second_head: str | None = None,
        on_second_head: Callable[[], None] | None = None,
        fail_post: bool = False,
        fail_readback: bool = False,
    ):
        self.head = head
        self.second_head = second_head
        self.on_second_head = on_second_head
        self.fail_post = fail_post
        self.fail_readback = fail_readback
        self.posts = 0
        self.head_reads = 0

    def current_head(self, credential: str, source: object, number: int) -> str:
        assert credential == "fixture-comment-credential"
        assert number == 7
        self.head_reads += 1
        if self.head_reads == 2 and self.on_second_head is not None:
            self.on_second_head()
        if self.second_head is not None and self.head_reads >= 2:
            return self.second_head
        return self.head

    def post_comment(self, credential: str, source: object, number: int, body: str) -> int:
        self.posts += 1
        assert credential == "fixture-comment-credential"
        assert body == "Review the error path."
        if self.fail_post:
            raise OSError("simulated ambiguous transport")
        return 123

    def read_comment(self, credential: str, source: object, number: int, comment_id: int) -> str:
        if self.fail_readback:
            raise OSError("simulated readback failure")
        assert comment_id == 123
        return "Review the error path."


def _approved(client: TestClient, gateway: ToolGateway) -> tuple[UUID, UUID]:
    project_id, run_id, _, source = _setup(client, gateway)
    approval_id = propose(
        gateway,
        project_id,
        run_id,
        ProposalInput(
            source_id=source.source_id,
            number=7,
            comment="Review the error path.",
            idempotency_key=uuid4(),
        ),
        _observation(source),
    )["approval_id"]
    response = client.post(
        f"/api/projects/{project_id}/approvals/{approval_id}/decision",
        json={"decision": "approved"},
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 200, response.text
    return project_id, approval_id


def _events(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT event_type,payload FROM events WHERE project_id=:project_id "
                    "AND payload->>'approval_id'=:approval_id "
                    "AND event_type LIKE 'github_pr_comment_%' ORDER BY created_at,id"
                ),
                {"project_id": project_id, "approval_id": str(approval_id)},
            ).mappings()
        ]


def test_approved_comment_has_one_durable_attempt_and_exact_readback(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, approval_id = _approved(client, gateway)
    transport = OfflineTransport()
    result = dispatch(gateway, project_id, approval_id, transport=transport)
    assert result["status"] == "done"
    assert result["comment_id"] == 123
    assert result["retry_allowed"] is False
    assert status(gateway, project_id, approval_id)["status"] == "done"
    assert transport.posts == 1
    with pytest.raises(Conflict, match="already attempted"):
        dispatch(gateway, project_id, approval_id, transport=transport)
    assert transport.posts == 1
    events = _events(gateway, project_id, approval_id)
    assert [item["event_type"] for item in events] == [
        "github_pr_comment_proposed",
        "github_pr_comment_attempted",
        "github_pr_comment_post_observed",
        "github_pr_comment_readback_started",
        "github_pr_comment_receipt",
    ]
    assert "Review the error path." not in json.dumps(events)
    assert "fixture-comment-credential" not in json.dumps(events)


@pytest.mark.parametrize(
    "transport,expected_posts,reason",
    [
        (OfflineTransport(second_head="c" * 40), 0, "request_not_sent"),
        (OfflineTransport(fail_post=True), 1, "post_ambiguous"),
        (OfflineTransport(fail_readback=True), 1, "readback_unverified"),
    ],
)
def test_stale_head_or_uncertain_network_never_retries(
    client: TestClient,
    gateway: ToolGateway,
    transport: OfflineTransport,
    expected_posts: int,
    reason: str,
) -> None:
    project_id, approval_id = _approved(client, gateway)
    result = dispatch(gateway, project_id, approval_id, transport=transport)
    assert result["status"] == ("failed" if expected_posts == 0 else "unknown")
    assert result["reason_code"] == reason
    assert result["retry_allowed"] is False
    assert transport.posts == expected_posts
    assert status(gateway, project_id, approval_id)["status"] == result["status"]
    with pytest.raises(Conflict, match="already attempted"):
        dispatch(gateway, project_id, approval_id, transport=transport)
    assert transport.posts == expected_posts


def test_project_and_source_cannot_change_during_final_head_check_or_post(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, approval_id = _approved(client, gateway)
    with gateway.engine.connect() as connection:
        source_id = connection.scalar(
            text("SELECT id FROM project_sources WHERE project_id=:project_id LIMIT 1"),
            {"project_id": project_id},
        )

    def try_remove_source() -> None:
        with pytest.raises(OperationalError):
            with gateway.engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                connection.execute(
                    text("DELETE FROM project_sources WHERE project_id=:project_id AND id=:source_id"),
                    {"project_id": project_id, "source_id": source_id},
                )
        with pytest.raises(OperationalError):
            with gateway.engine.begin() as connection:
                connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                connection.execute(
                    text("UPDATE projects SET status='archived' WHERE id=:project_id"),
                    {"project_id": project_id},
                )

    transport = OfflineTransport(on_second_head=try_remove_source)
    result = dispatch(gateway, project_id, approval_id, transport=transport)
    assert result["status"] == "done"
    assert transport.posts == 1


def test_revoked_role_cannot_dispatch_approved_comment(client: TestClient, gateway: ToolGateway) -> None:
    project_id, approval_id = _approved(client, gateway)
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:project_id "
                "AND id=(SELECT profile_id FROM runs WHERE project_id=:project_id "
                "AND id=(SELECT run_id FROM approval_requests WHERE id=:approval_id))"
            ),
            {"project_id": project_id, "approval_id": approval_id},
        )
    transport = OfflineTransport()
    with pytest.raises(Conflict, match="permission changed"):
        dispatch(gateway, project_id, approval_id, transport=transport)
    assert transport.head_reads == 0
    assert transport.posts == 0
    assert status(gateway, project_id, approval_id)["status"] == "not_attempted"


def test_revocation_after_attempt_records_known_not_sent(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, approval_id = _approved(client, gateway)
    original = comment_dispatch._active_role
    checks = 0

    def recheck(owner: ToolGateway, connection: Connection, proposal: CommentProposal) -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            with gateway.engine.begin() as other:
                other.execute(
                    text(
                        "UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:project_id "
                        "AND id=(SELECT profile_id FROM runs WHERE project_id=:project_id "
                        "AND id=:run_id)"
                    ),
                    {"project_id": project_id, "run_id": proposal.run_id},
                )
        original(owner, connection, proposal)

    monkeypatch.setattr(comment_dispatch, "_active_role", recheck)
    transport = OfflineTransport()
    result = dispatch(gateway, project_id, approval_id, transport=transport)
    assert checks == 3
    assert result["status"] == "failed"
    assert result["reason_code"] == "request_not_sent"
    assert transport.posts == 0
    assert status(gateway, project_id, approval_id) == result


def test_saved_not_sent_observation_survives_receipt_failure(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, approval_id = _approved(client, gateway)

    def fail_receipt(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise OSError("simulated receipt write failure")

    monkeypatch.setattr(comment_dispatch, "_record_receipt", fail_receipt)
    transport = OfflineTransport(second_head="c" * 40)
    with pytest.raises(OSError, match="receipt write failure"):
        dispatch(gateway, project_id, approval_id, transport=transport)
    saved = status(gateway, project_id, approval_id)
    assert saved["status"] == "failed"
    assert saved["reason_code"] == "request_not_sent"
    assert saved["retry_allowed"] is False
    assert transport.posts == 0
