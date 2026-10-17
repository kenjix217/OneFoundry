"""Offline PR comment proposals bind an exact run, source, and observed head."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.github_pr_comment_contract import CommentProposal, CurrentTarget, SavedSource
from onefoundry.github_pr_comment_proposal import (
    ProposalInput,
    pending_source_current,
    propose,
)
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_role_tool_admission_postgres import _post, _run

PIN = "a" * 40
HEAD = "b" * 40
TOOL_ID = "github_pr_comment_v1"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


@pytest.fixture
def gateway(settings: Settings) -> Iterator[ToolGateway]:
    engine = create_engine(settings.database_url)
    try:
        yield ToolGateway(engine, "operator", live_text_settings=settings)
    finally:
        engine.dispose()


def _setup(client: TestClient, gateway: ToolGateway) -> tuple[UUID, UUID, UUID, SavedSource]:
    # This offline module is not yet in the public tool registry. Give its
    # running profile and pinned snapshot the future tool ID in the fixture.
    project_id, run_id, profile_id = _run(client, gateway, tools=["github_pr_read_v1"])
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE profiles SET allowed_tools=CAST(:tools AS jsonb) "
                "WHERE project_id=:project_id AND id=:profile_id"
            ),
            {"tools": json.dumps([TOOL_ID]), "project_id": project_id, "profile_id": profile_id},
        )
        connection.execute(
            text(
                "INSERT INTO run_profile_snapshots "
                "(id,project_id,run_id,actor_id,transition_index,profile_id,profile_name,"
                "profile_model,profile_instructions,memory_scope,allowed_tools,autonomy_level) "
                "SELECT :id,project_id,run_id,actor_id,transition_index+1,profile_id,profile_name,"
                "profile_model,profile_instructions,memory_scope,CAST(:tools AS jsonb),autonomy_level "
                "FROM run_profile_snapshots WHERE project_id=:project_id AND run_id=:run_id "
                "AND transition_index=0"
            ),
            {"id": uuid4(), "tools": json.dumps([TOOL_ID]), "project_id": project_id, "run_id": run_id},
        )
    path = f"/api/projects/{project_id}"
    account = _post(
        client,
        f"{path}/github/accounts",
        {"label": "Offline comment fixture", "token": "fixture-comment-credential"},
    )
    with patch("onefoundry.source_store.resolve_revision", return_value=("main", PIN)):
        source = _post(
            client,
            f"{path}/sources/github",
            {"account_id": str(account["id"]), "owner": "sample", "repo": "project", "branch": "main"},
        )
    saved = SavedSource(
        project_id=project_id,
        source_id=UUID(str(source["id"])),
        account_id=UUID(str(account["id"])),
        owner="sample",
        repo="project",
        branch="main",
        saved_revision=PIN,
    )
    return project_id, run_id, profile_id, saved


def _observation(source: SavedSource, *, head: str = HEAD) -> CurrentTarget:
    return CurrentTarget(
        project_id=source.project_id,
        project_status="active",
        source=source,
        pr_number=7,
        head_sha=head,
    )


def _approval(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> dict[str, object]:
    with gateway.engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM approval_requests WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": approval_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_proposal_persists_exact_approval_and_hash_only_event(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, _, source = _setup(client, gateway)
    data = ProposalInput(
        source_id=source.source_id,
        number=7,
        comment="Please check the error path.",
        idempotency_key=uuid4(),
    )
    observation = _observation(source)
    result = propose(gateway, project_id, run_id, data, observation)
    assert result["status"] == "pending"
    approval_id = result["approval_id"]
    assert isinstance(approval_id, UUID)
    approval = _approval(gateway, project_id, approval_id)
    content = approval["content"]
    assert isinstance(content, str)
    proposal = CommentProposal.model_validate_json(
        json.dumps({**json.loads(content), "proposal_digest": approval["proposal_digest"]})
    )
    assert proposal.source == source
    assert proposal.head_sha == HEAD
    assert proposal.comment == data.comment
    assert proposal.idempotency_key == data.idempotency_key
    assert approval["target"] == (
        "Comment on GitHub PR #7 in sample/project (commits may change before posting)"
    )
    assert approval["proposal_digest"] == proposal.proposal_digest
    assert approval["action_type"] == "other"
    with gateway.engine.connect() as connection:
        assert pending_source_current(connection, project_id, approval) is True
        events = (
            connection.execute(
                text(
                    "SELECT payload FROM events WHERE project_id=:project_id AND run_id=:run_id "
                    "AND event_type='github_pr_comment_proposed'"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .all()
        )
    assert len(events) == 1
    assert events[0]["payload"]["approval_id"] == str(approval_id)
    assert events[0]["payload"]["proposal_digest"] == proposal.proposal_digest
    encoded_events = json.dumps([row["payload"] for row in events])
    assert data.comment not in encoded_events
    assert "fixture-comment-credential" not in encoded_events + str(approval)

    assert propose(gateway, project_id, run_id, data, observation) == result
    with pytest.raises(Conflict, match="already bound"):
        propose(gateway, project_id, run_id, data.model_copy(update={"comment": "Different"}), observation)
    with pytest.raises(Conflict, match="already bound"):
        propose(gateway, project_id, run_id, data, _observation(source, head="c" * 40))


def test_proposal_rejects_wrong_owner_observation_and_revoked_tool(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, profile_id, source = _setup(client, gateway)
    data = ProposalInput(
        source_id=source.source_id, number=7, comment="Review this.", idempotency_key=uuid4()
    )
    other_project = UUID(str(_post(client, "/api/projects", {"name": "Other comment project"})["id"]))
    with pytest.raises(NotFound, match="run not found"):
        propose(gateway, other_project, run_id, data, _observation(source))
    with pytest.raises(Conflict, match="observation"):
        propose(
            gateway,
            project_id,
            run_id,
            data,
            CurrentTarget(
                project_id=project_id, project_status="archived", source=source, pr_number=7, head_sha=HEAD
            ),
        )
    with pytest.raises(Conflict, match="observation"):
        propose(
            gateway,
            project_id,
            run_id,
            data,
            CurrentTarget(
                project_id=project_id, project_status="active", source=source, pr_number=8, head_sha=HEAD
            ),
        )
    with gateway.engine.begin() as connection:
        connection.execute(
            text("UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:project_id AND id=:id"),
            {"project_id": project_id, "id": profile_id},
        )
    with pytest.raises(Conflict, match="not allowed"):
        propose(gateway, project_id, run_id, data, _observation(source))
    with gateway.engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE project_id=:project_id "
                "AND event_type='github_pr_comment_proposed'"
            ),
            {"project_id": project_id},
        )
    assert count == 0


def test_pending_source_check_rejects_tampering_and_deleted_source(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, _, source = _setup(client, gateway)
    data = ProposalInput(
        source_id=source.source_id, number=7, comment="Review this.", idempotency_key=uuid4()
    )
    approval_id = propose(gateway, project_id, run_id, data, _observation(source))["approval_id"]
    assert isinstance(approval_id, UUID)
    approval = _approval(gateway, project_id, approval_id)
    with gateway.engine.connect() as connection:
        assert pending_source_current(connection, project_id, {**approval, "target": "other"}) is False
        assert (
            pending_source_current(connection, project_id, {**approval, "proposal_digest": "0" * 64}) is None
        )
    response = client.delete(
        f"/api/projects/{project_id}/sources/{source.source_id}",
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 204
    with gateway.engine.connect() as connection:
        assert pending_source_current(connection, project_id, approval) is False
