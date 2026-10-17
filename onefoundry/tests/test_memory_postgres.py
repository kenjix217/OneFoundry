"""Real PostgreSQL checks for local DreamCycle review and memory visibility."""

import json
import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from onefoundry.main import create_app
from onefoundry.memory_schemas import CandidateReview, ResultCreate
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict, ToolGateway


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def post(client: TestClient, path: str, body: Mapping[str, object] | None = None) -> Response:
    return cast(Response, client.post(path, json=body, headers={"origin": "http://localhost:8765"}))


def completed_run(
    client: TestClient, project_id: str | None = None, label: str = "Local task"
) -> tuple[str, str]:
    if project_id is None:
        project_id = post(client, "/api/projects", {"name": label}).json()["id"]
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    profile_id = profiles[0]["id"]
    path = f"/api/projects/{project_id}"
    order = post(
        client,
        f"{path}/work-orders",
        {"title": label, "instructions": "Manual local result", "profile_id": profile_id},
    ).json()
    assert post(client, f"{path}/work-orders/{order['id']}/ready").status_code == 200
    run = post(client, f"{path}/work-orders/{order['id']}/runs").json()
    assert post(client, f"{path}/runs/{run['id']}/finish", {"outcome": "done"}).status_code == 200
    return project_id, run["id"]


def result_candidate(
    client: TestClient,
    project_id: str,
    run_id: str,
    summary: str,
    scope: str,
) -> tuple[dict[str, object], dict[str, object]]:
    path = f"/api/projects/{project_id}"
    response = post(client, f"{path}/runs/{run_id}/results", {"summary": summary, "scope": scope})
    assert response.status_code == 201, response.text
    result = cast(dict[str, object], response.json())
    candidate_response = post(client, f"{path}/results/{result['id']}/candidates", {})
    assert candidate_response.status_code == 201, candidate_response.text
    return result, cast(dict[str, object], candidate_response.json())


def test_review_publication_citation_and_scope(client: TestClient, settings: Settings) -> None:
    first, first_run = completed_run(client, label="First memory")
    second, second_run = completed_run(client, label="Second memory")
    first_path = f"/api/projects/{first}"
    second_path = f"/api/projects/{second}"
    project_result, project_candidate = result_candidate(
        client, first, first_run, "Copper notebook is project only", "project"
    )
    company_result, company_candidate = result_candidate(
        client, second, second_run, "Silver notebook is company shared", "company"
    )
    assert project_result["source_kind"] == "operator_entered"
    assert project_result["actor_id"] == "operator"
    assert project_candidate["status"] == "pending"
    assert client.get(f"{first_path}/memory?query=Copper").json() == []
    assert client.get(f"{second_path}/memory?query=Silver").json() == []
    assert client.get(f"{first_path}/results").json()[0]["id"] == project_result["id"]
    assert client.get(f"{first_path}/candidates?status=pending").json()[0]["id"] == project_candidate["id"]
    assert client.get(f"{second_path}/results/{project_result['id']}").status_code == 404
    assert post(client, f"{second_path}/results/{project_result['id']}/candidates", {}).status_code == 404
    assert (
        post(
            client, f"{second_path}/candidates/{project_candidate['id']}/review", {"decision": "approved"}
        ).status_code
        == 404
    )
    project_review = post(
        client, f"{first_path}/candidates/{project_candidate['id']}/review", {"decision": "approved"}
    )
    company_review = post(
        client, f"{second_path}/candidates/{company_candidate['id']}/review", {"decision": "approved"}
    )
    assert project_review.status_code == 200 and company_review.status_code == 200
    assert project_review.json()["reviewed_by_actor_id"] == "operator"
    assert project_review.json()["reviewed_at"]
    first_memory = client.get(f"{first_path}/memory?query=notebook").json()
    second_memory = client.get(f"{second_path}/memory?query=notebook").json()
    assert {item["text"] for item in first_memory} == {
        "Copper notebook is project only",
        "Silver notebook is company shared",
    }
    assert {item["text"] for item in second_memory} == {"Silver notebook is company shared"}
    assert all(item["score"] == 1 for item in first_memory)
    assert all(item["source_result_id"] and item["source_candidate_id"] for item in first_memory)
    assert all(item["actor_id"] == "operator" and item["created_at"] for item in first_memory)
    assert client.get(f"{first_path}/memory").json()
    assert all(item["score"] == 0 for item in client.get(f"{first_path}/memory").json())
    assert client.get(f"{first_path}/memory?query=absent").json() == []
    assert client.get(f"{first_path}/memory?limit=0").status_code == 422

    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        event_rows = (
            connection.execute(
                text(
                    "SELECT event_type, payload, actor_id FROM events WHERE project_id = :project_id "
                    "AND event_type LIKE 'dreamcycle_%' ORDER BY created_at"
                ),
                {"project_id": UUID(first)},
            )
            .mappings()
            .all()
        )
    assert {row["event_type"] for row in event_rows} == {
        "dreamcycle_candidate_created",
        "dreamcycle_candidate_reviewed",
    }
    assert all(row["actor_id"] == "operator" for row in event_rows)
    assert all("Copper" not in json.dumps(row["payload"]) for row in event_rows)
    engine.dispose()


def test_rejection_and_invalid_inputs_never_publish(client: TestClient) -> None:
    project, run = completed_run(client, label="Rejected memory")
    path = f"/api/projects/{project}"
    result, candidate = result_candidate(client, project, run, "Amber notebook stays rejected", "company")
    assert post(client, f"{path}/results/{result['id']}/candidates", {}).status_code == 409
    rejected = post(client, f"{path}/candidates/{candidate['id']}/review", {"decision": "rejected"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert (
        post(client, f"{path}/candidates/{candidate['id']}/review", {"decision": "approved"}).status_code
        == 409
    )
    assert client.get(f"{path}/memory?query=Amber").json() == []
    assert client.get(f"{path}/candidates?status=rejected").json()[0]["id"] == candidate["id"]
    assert (
        post(client, f"{path}/runs/{run}/results", {"summary": "Again", "scope": "project"}).status_code
        == 409
    )
    assert (
        post(client, f"{path}/runs/{uuid4()}/results", {"summary": "Missing", "scope": "project"}).status_code
        == 404
    )
    assert post(client, f"{path}/runs/{run}/results", {"summary": " ", "scope": "project"}).status_code == 422
    assert (
        post(
            client, f"{path}/runs/{run}/results", {"summary": "api_key=unsafe", "scope": "project"}
        ).status_code
        == 422
    )
    assert (
        post(
            client, f"{path}/runs/{run}/results", {"summary": "[TOOL_REQUEST:bad]", "scope": "project"}
        ).status_code
        == 422
    )
    assert (
        post(client, f"{path}/runs/{run}/results", {"summary": "Okay", "scope": "global"}).status_code == 422
    )
    assert client.get(f"{path}/memory?query=api_key=unsafe").status_code == 422


def test_requires_successful_completed_run_and_http_boundary(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Unfinished source"}).json()["id"]
    path = f"/api/projects/{project}"
    profile = client.get(f"{path}/profiles").json()[0]
    order = post(
        client,
        f"{path}/work-orders",
        {"title": "Pending", "instructions": "Local", "profile_id": profile["id"]},
    ).json()
    assert post(client, f"{path}/work-orders/{order['id']}/ready").status_code == 200
    run = post(client, f"{path}/work-orders/{order['id']}/runs").json()
    assert (
        post(
            client, f"{path}/runs/{run['id']}/results", {"summary": "Not complete", "scope": "project"}
        ).status_code
        == 409
    )
    assert post(client, f"{path}/runs/{run['id']}/finish", {"outcome": "failed"}).status_code == 200
    assert (
        post(
            client, f"{path}/runs/{run['id']}/results", {"summary": "Failed", "scope": "project"}
        ).status_code
        == 409
    )
    assert client.get(f"/api/projects/{uuid4()}/memory").status_code == 404
    assert client.get(f"/api/projects/{uuid4()}/candidates").status_code == 404
    assert client.get(f"/api/projects/{uuid4()}/results").status_code == 404
    assert client.get(f"{path}/memory", headers={"host": "bad.example"}).status_code == 400
    assert (
        client.post(
            f"{path}/runs/{run['id']}/results",
            json={"summary": "Local", "scope": "project"},
            headers={"origin": "http://evil.example"},
        ).status_code
        == 403
    )


def test_publication_db_links_and_review_race(client: TestClient, settings: Settings) -> None:
    project, run_one = completed_run(client, label="Link one")
    _, run_two = completed_run(client, project_id=project, label="Link two")
    path = f"/api/projects/{project}"
    result_one, candidate_one = result_candidate(client, project, run_one, "First source", "project")
    result_two, _ = result_candidate(client, project, run_two, "Second source", "project")
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator")

    def review() -> str:
        try:
            gateway.review_candidate(
                UUID(project), UUID(cast(str, candidate_one["id"])), CandidateReview(decision="approved")
            )
            return "approved"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: review(), range(2)))
    assert sorted(outcomes) == ["approved", "conflict"]
    with pytest.raises(IntegrityError, match="matching approved candidate"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_items "
                    "(id, project_id, actor_id, scope, text, source_result_id, source_candidate_id) "
                    "VALUES (:id, :project_id, 'operator', 'project', 'First source', "
                    ":source_result_id, :source_candidate_id)"
                ),
                {
                    "id": uuid4(),
                    "project_id": UUID(project),
                    "source_result_id": UUID(cast(str, result_two["id"])),
                    "source_candidate_id": UUID(cast(str, candidate_one["id"])),
                },
            )
    with pytest.raises(IntegrityError, match="memory publication requires matching approved candidate"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_items "
                    "(id, project_id, actor_id, scope, text, source_result_id, source_candidate_id) "
                    "VALUES (:id, :project_id, 'operator', 'company', 'Changed text', "
                    ":source_result_id, :source_candidate_id)"
                ),
                {
                    "id": uuid4(),
                    "project_id": UUID(project),
                    "source_result_id": UUID(cast(str, result_one["id"])),
                    "source_candidate_id": UUID(cast(str, candidate_one["id"])),
                },
            )
    with engine.connect() as connection:
        candidate_link = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'memory_items'::regclass AND contype = 'f' "
                "AND conname = 'memory_items_candidate_result_fk'"
            )
        )
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE project_id = :project_id "
                "AND event_type = 'dreamcycle_candidate_reviewed'"
            ),
            {"project_id": UUID(project)},
        )
    assert candidate_link is not None and "source_result_id" in candidate_link
    assert count == 1
    assert len(client.get(f"{path}/memory?query=First").json()) == 1
    engine.dispose()


def test_event_failure_rolls_back_result(client: TestClient, settings: Settings) -> None:
    project, run = completed_run(client, label="Atomic result")
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator")
    with patch.object(gateway, "_event", side_effect=RuntimeError("event unavailable")):
        with pytest.raises(RuntimeError, match="event unavailable"):
            gateway.create_result(
                UUID(project), UUID(run), ResultCreate(summary="Durable candidate", scope="project")
            )
    assert client.get(f"/api/projects/{project}/results").json() == []
    engine.dispose()


def test_exact_word_survives_more_than_500_newer_substring_rows(
    client: TestClient, settings: Settings
) -> None:
    project, run = completed_run(client, label="Older exact word")
    path = f"/api/projects/{project}"
    _, candidate = result_candidate(client, project, run, "A cat sleeps here", "project")
    reviewed = post(client, f"{path}/candidates/{candidate['id']}/review", {"decision": "approved"})
    assert reviewed.status_code == 200
    assert [row["text"] for row in client.get(f"{path}/memory?query=cat").json()] == ["A cat sleeps here"]

    profile_id = client.get(f"{path}/profiles").json()[0]["id"]
    project_uuid = UUID(project)
    profile_uuid = UUID(profile_id)
    fillers = [
        {
            "project_id": project_uuid,
            "profile_id": profile_uuid,
            "order_id": uuid4(),
            "run_id": uuid4(),
            "result_id": uuid4(),
            "candidate_id": uuid4(),
            "memory_id": uuid4(),
        }
        for _ in range(501)
    ]
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO work_orders "
                "(id, project_id, actor_id, title, instructions, profile_id, status) "
                "VALUES (:order_id, :project_id, 'operator', 'Fixture', 'Local', :profile_id, 'done')"
            ),
            fillers,
        )
        connection.execute(
            text(
                "INSERT INTO runs "
                "(id, project_id, actor_id, work_order_id, profile_id, status, mode, finished_at) "
                "VALUES (:run_id, :project_id, 'operator', :order_id, :profile_id, "
                "'done', 'manual_planning', clock_timestamp())"
            ),
            fillers,
        )
        connection.execute(
            text(
                "INSERT INTO run_results "
                "(id, project_id, actor_id, run_id, summary, scope) "
                "VALUES (:result_id, :project_id, 'operator', :run_id, 'caterpillar', 'company')"
            ),
            fillers,
        )
        connection.execute(
            text(
                "INSERT INTO dreamcycle_candidates "
                "(id, project_id, actor_id, source_result_id, summary, scope, status) "
                "VALUES (:candidate_id, :project_id, 'operator', :result_id, 'caterpillar', "
                "'company', 'pending')"
            ),
            fillers,
        )
        connection.execute(
            text(
                "UPDATE dreamcycle_candidates SET status = 'approved', "
                "reviewed_at = clock_timestamp(), reviewed_by_actor_id = 'operator' "
                "WHERE project_id = :project_id AND id = :candidate_id"
            ),
            fillers,
        )
        connection.execute(
            text(
                "INSERT INTO memory_items "
                "(id, project_id, actor_id, scope, text, source_result_id, source_candidate_id) "
                "VALUES (:memory_id, :project_id, 'operator', 'company', 'caterpillar', "
                ":result_id, :candidate_id)"
            ),
            fillers,
        )

    response = client.get(f"{path}/memory?query=cat")
    assert response.status_code == 200
    assert [row["text"] for row in response.json()] == ["A cat sleeps here"]
    assert [row["text"] for row in client.get(f"{path}/memory?scope=project").json()] == ["A cat sleeps here"]
    assert len(client.get(f"{path}/memory?scope=company").json()) == 20
    assert len(client.get(f"{path}/memory").json()) == 20
    engine.dispose()


def test_candidate_db_source_binding_and_one_shot_review(client: TestClient, settings: Settings) -> None:
    project, run = completed_run(client, label="Candidate DB guard")
    path = f"/api/projects/{project}"
    result_response = post(
        client, f"{path}/runs/{run}/results", {"summary": "Verified local wording", "scope": "project"}
    )
    assert result_response.status_code == 201
    result = result_response.json()
    engine = create_engine(settings.database_url)
    row = {
        "id": uuid4(),
        "project_id": UUID(project),
        "source_result_id": UUID(result["id"]),
    }
    with pytest.raises(IntegrityError, match="candidate must match source result"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dreamcycle_candidates "
                    "(id, project_id, actor_id, source_result_id, summary, scope, status) "
                    "VALUES (:id, :project_id, 'operator', :source_result_id, "
                    "'Misleading wording', 'project', 'pending')"
                ),
                row,
            )
    with pytest.raises(IntegrityError, match="candidate must match source result"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dreamcycle_candidates "
                    "(id, project_id, actor_id, source_result_id, summary, scope, status) "
                    "VALUES (:id, :project_id, 'operator', :source_result_id, "
                    "'Verified local wording', 'company', 'pending')"
                ),
                row,
            )
    with pytest.raises(IntegrityError, match="candidate must begin pending"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dreamcycle_candidates "
                    "(id, project_id, actor_id, source_result_id, summary, scope, status, "
                    "reviewed_at, reviewed_by_actor_id) "
                    "VALUES (:id, :project_id, 'operator', :source_result_id, "
                    "'Verified local wording', 'project', 'approved', clock_timestamp(), 'operator')"
                ),
                row,
            )
    candidate_response = post(client, f"{path}/results/{result['id']}/candidates", {})
    assert candidate_response.status_code == 201
    candidate_id = candidate_response.json()["id"]
    review_response = post(client, f"{path}/candidates/{candidate_id}/review", {"decision": "approved"})
    assert review_response.status_code == 200
    with pytest.raises(IntegrityError, match="candidate decision is one-shot"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE dreamcycle_candidates SET status = 'rejected', "
                    "reviewed_at = clock_timestamp(), reviewed_by_actor_id = 'operator' "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"project_id": UUID(project), "id": UUID(candidate_id)},
            )
    assert client.get(f"{path}/candidates?status=approved").json()[0]["id"] == candidate_id
    assert client.get(f"{path}/memory?query=Verified").json()[0]["source_candidate_id"] == candidate_id
    engine.dispose()
