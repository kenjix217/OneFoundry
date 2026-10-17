"""An admitted role can request QA review without changing run ownership."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Barrier, Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.role_qa_review_request import TOOL_ID, invoke, parse_arguments
from onefoundry.role_tool_admission import ToolDenied, ToolInputError
from onefoundry.settings import Settings
from onefoundry.tool_gateway import NotFound, ToolGateway
from tests.test_role_tool_admission_postgres import _post, _run, _tool_events


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


def _profile(client: TestClient, project_id: UUID, name: str) -> UUID:
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    return UUID(next(profile["id"] for profile in profiles if profile["name"] == name))


def _invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    source_profile_id: UUID,
    target_profile_id: UUID,
    call_id: str,
    note: object = "Please review the retry tests.",
) -> dict[str, Any]:
    return invoke(
        gateway,
        project_id,
        run_id,
        source_profile_id,
        call_id,
        {"target_profile_id": str(target_profile_id), "note": note},
        1,
    )


def test_request_is_visible_to_operator_and_does_not_handoff_or_complete(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, researcher_id = _run(client, gateway, tools=[TOOL_ID])
    qa_id = _profile(client, project_id, "QA")
    note = "Review the retry path and attach the failing case before signoff."
    receipt = _invoke(gateway, project_id, run_id, researcher_id, qa_id, "qa-request-1", note)
    assert receipt["result"]["status"] == "proposed"
    assert receipt["result"]["target_profile_id"] == str(qa_id)
    assert note not in json.dumps(receipt)

    detail_response = client.get(f"/api/projects/{project_id}/runs/{run_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    requests = [event for event in detail["events"] if event["event_type"] == "qa_review_requested"]
    assert len(requests) == 1
    request = requests[0]
    assert request["payload"]["note"] == note
    assert request["payload"]["target_profile_id"] == str(qa_id)
    assert request["actor_id"] == f"profile:{researcher_id}"
    assert request["project_id"] == str(project_id)
    assert request["run_id"] == str(run_id)
    assert request["work_order_id"] == detail["work_order_id"]
    assert detail["status"] == "running"
    assert detail["profile_id"] == str(researcher_id)
    with gateway.engine.connect() as connection:
        assigned = connection.scalar(
            text("SELECT profile_id FROM work_orders WHERE project_id=:project_id AND id=:order_id"),
            {"project_id": project_id, "order_id": UUID(detail["work_order_id"])},
        )
    assert assigned == researcher_id
    assert [event["event_type"] for event in _tool_events(gateway, project_id, run_id)][-2:] == [
        "role_tool_started",
        "role_tool_finished",
    ]
    assert note not in json.dumps(_tool_events(gateway, project_id, run_id))
    with pytest.raises(ToolDenied, match="already attempted"):
        _invoke(gateway, project_id, run_id, researcher_id, qa_id, "qa-request-1", note)


@pytest.mark.parametrize("note", [" ", "x" * 2001, "password=private123", "bad\x00note", "bad\u202enote", 42])
def test_rejects_invalid_or_sensitive_note(note: object) -> None:
    with pytest.raises(ToolInputError):
        parse_arguments({"target_profile_id": str(uuid4()), "note": note})


@pytest.mark.parametrize("target", ["bad-uuid", str(UUID(int=0)), "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
def test_rejects_invalid_target(target: str) -> None:
    with pytest.raises(ToolInputError):
        parse_arguments({"target_profile_id": target, "note": "Review."})


def test_denies_foreign_or_non_qa_target_and_revoked_tool(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, researcher_id = _run(client, gateway, tools=[TOOL_ID])
    qa_id = _profile(client, project_id, "QA")
    other_project = _post(client, "/api/projects", {"name": f"Other {uuid4()}"})
    other_project_id = UUID(str(other_project["id"]))
    foreign_qa_id = _profile(client, other_project_id, "QA")
    with pytest.raises(NotFound, match="run not found"):
        _invoke(gateway, other_project_id, run_id, researcher_id, foreign_qa_id, "foreign-run")
    with pytest.raises(ToolDenied, match="target must be"):
        _invoke(gateway, project_id, run_id, researcher_id, foreign_qa_id, "foreign")
    with pytest.raises(ToolDenied, match="target must be"):
        _invoke(gateway, project_id, run_id, researcher_id, researcher_id, "not-qa")
    response = client.patch(
        f"/api/projects/{project_id}/profiles/{researcher_id}",
        json={"allowed_tools": []},
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 200, response.text
    with pytest.raises(ToolDenied, match="not admitted"):
        _invoke(gateway, project_id, run_id, researcher_id, qa_id, "revoked")
    with gateway.engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE project_id=:project_id AND run_id=:run_id "
                "AND event_type='qa_review_requested'"
            ),
            {"project_id": project_id, "run_id": run_id},
        )
    assert count == 0


def test_event_failure_rolls_back_request(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, researcher_id = _run(client, gateway, tools=[TOOL_ID])
    qa_id = _profile(client, project_id, "QA")
    original = gateway._event

    def fail_finish(*args: object, **kwargs: object) -> None:
        if args[4] == "role_tool_finished":
            raise RuntimeError("synthetic event failure")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway, "_event", fail_finish)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        _invoke(gateway, project_id, run_id, researcher_id, qa_id, "rollback")
    with gateway.engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE project_id=:project_id AND run_id=:run_id "
                "AND event_type='qa_review_requested'"
            ),
            {"project_id": project_id, "run_id": run_id},
        )
    assert count == 0
    assert _tool_events(gateway, project_id, run_id) == []


def _request_event(client: TestClient, gateway: ToolGateway) -> tuple[UUID, UUID, UUID, UUID]:
    project_id, run_id, researcher_id = _run(client, gateway, tools=[TOOL_ID])
    qa_id = _profile(client, project_id, "QA")
    _invoke(gateway, project_id, run_id, researcher_id, qa_id, f"qa-{uuid4()}")
    detail = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()
    request = next(event for event in detail["events"] if event["event_type"] == "qa_review_requested")
    return project_id, run_id, UUID(request["id"]), qa_id


def _draft_path(project_id: UUID, run_id: UUID, request_id: UUID) -> str:
    return f"/api/projects/{project_id}/runs/{run_id}/qa-review-requests/{request_id}/draft"


def post(client: TestClient, path: str, body: Mapping[str, object]) -> Response:
    return client.post(path, json=body, headers={"origin": "http://localhost:8765"})


def test_operator_creates_one_linked_qa_draft_without_handoff(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, request_id, qa_id = _request_event(client, gateway)
    path = _draft_path(project_id, run_id, request_id)
    empty = client.get(path)
    assert empty.status_code == 200 and empty.json() is None
    body = {"title": "Review research output", "instructions": "Check the cited source and result."}
    created = client.post(path, json=body, headers={"origin": "http://localhost:8765"})
    assert created.status_code == 200, created.text
    draft = created.json()
    assert draft["project_id"] == str(project_id)
    assert draft["profile_id"] == str(qa_id)
    assert draft["status"] == "draft"
    assert draft["title"] == body["title"]
    assert draft["instructions"] == body["instructions"]
    reloaded = client.get(path)
    assert reloaded.status_code == 200 and reloaded.json() == draft
    assert post(client, path, body).json()["id"] == draft["id"]
    assert post(client, path, {**body, "instructions": "Different task"}).status_code == 409

    source = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()
    assert source["status"] == "running"
    assert source["profile_id"] != str(qa_id)
    with gateway.engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT actor_id,status FROM work_orders WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": UUID(draft["id"])},
            )
            .mappings()
            .one()
        )
        linked = (
            connection.execute(
                text(
                    "SELECT payload,actor_id,run_id FROM events WHERE project_id=:project_id "
                    "AND work_order_id=:order_id AND event_type='work_order_created'"
                ),
                {"project_id": project_id, "order_id": UUID(draft["id"])},
            )
            .mappings()
            .one()
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM runs WHERE project_id=:project_id AND work_order_id=:order_id"),
                {"project_id": project_id, "order_id": UUID(draft["id"])},
            )
            == 0
        )
    assert row["actor_id"] == "operator" and row["status"] == "draft"
    assert linked["actor_id"] == "operator" and linked["run_id"] is None
    assert linked["payload"]["source_qa_request_event_id"] == str(request_id)
    assert linked["payload"]["source_run_id"] == str(run_id)
    assert "Check the cited" not in json.dumps(linked["payload"])
    ready = post(client, f"/api/projects/{project_id}/work-orders/{draft['id']}/ready", {})
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert client.get(path).json()["status"] == "ready"
    replay = post(client, path, body)
    assert replay.status_code == 200
    assert replay.json()["id"] == draft["id"] and replay.json()["status"] == "ready"


def test_qa_draft_rejects_wrong_owner_and_bad_instructions(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, request_id, _ = _request_event(client, gateway)
    other_project_id, other_run_id, _, _ = _request_event(client, gateway)
    body = {"title": "Review", "instructions": "Check result."}
    assert post(client, _draft_path(project_id, run_id, uuid4()), body).status_code == 404
    assert post(client, _draft_path(project_id, other_run_id, request_id), body).status_code == 404
    assert post(client, _draft_path(other_project_id, run_id, request_id), body).status_code == 404
    assert client.get(_draft_path(project_id, run_id, uuid4())).status_code == 404
    assert client.get(_draft_path(project_id, other_run_id, request_id)).status_code == 404
    assert client.get(_draft_path(other_project_id, run_id, request_id)).status_code == 404
    assert (
        post(client, _draft_path(project_id, run_id, request_id), {**body, "instructions": " "}).status_code
        == 422
    )
    assert (
        post(
            client, _draft_path(project_id, run_id, request_id), {**body, "instructions": "password=secret"}
        ).status_code
        == 422
    )
    assert (
        post(
            client,
            _draft_path(project_id, run_id, request_id),
            {**body, "instructions": "Use postgres://agent:pa55@db.internal/report"},
        ).status_code
        == 422
    )
    assert (
        post(client, _draft_path(project_id, run_id, request_id), {**body, "title": "x" * 161}).status_code
        == 422
    )
    assert client.get(_draft_path(project_id, run_id, request_id)).json() is None


def test_same_qa_request_is_one_draft_under_concurrent_posts(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, request_id, _ = _request_event(client, gateway)
    path = _draft_path(project_id, run_id, request_id)
    body = {"title": "QA task", "instructions": "Verify the output."}
    barrier = Barrier(2)

    def submit(_: int) -> tuple[int, str]:
        barrier.wait()
        response = client.post(path, json=body, headers={"origin": "http://localhost:8765"})
        return response.status_code, response.json()["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert responses[0] == responses[1]
    assert responses[0][0] == 200
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:project_id "
                    "AND event_type='work_order_created' "
                    "AND payload->>'source_qa_request_event_id'=:request_id"
                ),
                {"project_id": project_id, "request_id": str(request_id)},
            )
            == 1
        )


def test_archive_in_progress_blocks_qa_draft_then_rejects_it(
    client: TestClient, gateway: ToolGateway
) -> None:
    from onefoundry.qa_review_draft import QAReviewDraftCreate, create_from_request
    from onefoundry.tool_gateway import Conflict

    project_id, run_id, request_id, _ = _request_event(client, gateway)
    started = Event()

    def create() -> dict[str, Any]:
        started.set()
        return create_from_request(
            gateway,
            project_id,
            run_id,
            request_id,
            QAReviewDraftCreate(title="QA task", instructions="Check result."),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with gateway.engine.begin() as blocker:
            blocker.execute(
                text("UPDATE projects SET status='archived' WHERE id=:project_id"),
                {"project_id": project_id},
            )
            future = pool.submit(create)
            assert started.wait(timeout=2)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.3)
        with pytest.raises(Conflict, match="project is not active"):
            future.result(timeout=5)
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM work_orders WHERE project_id=:project_id AND title='QA task'"),
                {"project_id": project_id},
            )
            == 0
        )


def test_qa_draft_event_failure_rolls_back_order(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, request_id, _ = _request_event(client, gateway)
    original = gateway._event

    def fail_created(*args: object, **kwargs: object) -> None:
        if args[4] == "work_order_created":
            raise RuntimeError("synthetic event failure")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway, "_event", fail_created)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        from onefoundry.qa_review_draft import QAReviewDraftCreate, create_from_request

        create_from_request(
            gateway,
            project_id,
            run_id,
            request_id,
            QAReviewDraftCreate(title="QA task", instructions="Check result."),
        )
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM work_orders WHERE project_id=:project_id AND title='QA task'"),
                {"project_id": project_id},
            )
            == 0
        )
