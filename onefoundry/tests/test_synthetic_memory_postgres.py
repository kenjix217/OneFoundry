"""Disposable PostgreSQL proof of exact synthetic result lineage and review."""

import hashlib
import json
import os
from collections.abc import Iterator
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Connection, create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as value:
        yield value


def post(client: TestClient, path: str, body: dict[str, object]) -> Response:
    return cast(Response, client.post(path, json=body, headers={"origin": "http://localhost:8765"}))


def seed_project(client: TestClient) -> tuple[UUID, dict[str, UUID]]:
    project = post(client, "/api/projects", {"name": f"Memory fixture {uuid4()}"})
    assert project.status_code == 201
    project_id = UUID(project.json()["id"])
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    return project_id, {row["name"]: UUID(row["id"]) for row in profiles}


def seed_run(connection: Connection, project_id: UUID, profile_id: UUID, mode: str) -> tuple[UUID, UUID]:
    order_id, run_id = uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO work_orders (id,project_id,actor_id,title,instructions,profile_id,status) "
            "VALUES (:id,:project_id,'operator','Synthetic memory fixture',"
            "'Local fixture only',:profile_id,'done')"
        ),
        {"id": order_id, "project_id": project_id, "profile_id": profile_id},
    )
    connection.execute(
        text(
            "INSERT INTO runs (id,project_id,actor_id,work_order_id,profile_id,status,mode,finished_at) "
            "VALUES (:id,:project_id,'operator',:order_id,:profile_id,'done',:mode,clock_timestamp())"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "order_id": order_id,
            "profile_id": profile_id,
            "mode": mode,
        },
    )
    return order_id, run_id


def seed_sales(
    connection: Connection, project_id: UUID, profiles: dict[str, UUID], *, delivery_status: str
) -> tuple[UUID, UUID]:
    order_id, run_id = seed_run(connection, project_id, profiles["Sales"], "synthetic_text")
    output = "Synthetic GTM source for copper atlas"
    connection.execute(
        text(
            "INSERT INTO text_outputs "
            "(id,project_id,actor_id,run_id,profile_id,role,text,text_sha256) "
            "VALUES (:id,:project_id,:actor_id,:run_id,:profile_id,'GTM',:body,:digest)"
        ),
        {
            "id": uuid4(),
            "project_id": project_id,
            "actor_id": f"profile:{profiles['GTM']}",
            "run_id": run_id,
            "profile_id": profiles["GTM"],
            "body": output,
            "digest": hashlib.sha256(output.encode()).hexdigest(),
        },
    )
    approval_id, draft_id = uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO approval_requests "
            "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,proposal_digest,"
            "status,decided_at) VALUES "
            "(:id,:project_id,'operator',:order_id,:run_id,'email','sales-preview@onefoundry.invalid',"
            "'Synthetic preview',:digest,'approved',clock_timestamp())"
        ),
        {
            "id": approval_id,
            "project_id": project_id,
            "order_id": order_id,
            "run_id": run_id,
            "digest": "a" * 64,
        },
    )
    body = "Synthetic Sales delivery: copper atlas approved for local preview."
    connection.execute(
        text(
            "INSERT INTO sales_drafts "
            "(id,project_id,actor_id,source_run_id,run_id,approval_id,recipient,subject,body,"
            "body_sha256,delivery_status,sent_at) VALUES "
            "(:id,:project_id,:actor_id,:run_id,:run_id,:approval_id,"
            "'sales-preview@onefoundry.invalid','Copper atlas preview',:body,:digest,:status,"
            "CASE WHEN :status='delivered' THEN clock_timestamp() ELSE NULL END)"
        ),
        {
            "id": draft_id,
            "project_id": project_id,
            "actor_id": f"profile:{profiles['Sales']}",
            "run_id": run_id,
            "approval_id": approval_id,
            "body": body,
            "digest": hashlib.sha256(body.encode()).hexdigest(),
            "status": delivery_status,
        },
    )
    return run_id, draft_id


def seed_qa(connection: Connection, project_id: UUID, profiles: dict[str, UUID]) -> tuple[UUID, UUID]:
    _, run_id = seed_run(connection, project_id, profiles["QA"], "synthetic_code")
    engineer_id, qa_id = uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO runner_jobs (id,project_id,actor_id,run_id,stage,status,receipt_sha256) "
            "VALUES (:id,:project_id,'operator',:run_id,'engineer','done',:digest)"
        ),
        {"id": engineer_id, "project_id": project_id, "run_id": run_id, "digest": "b" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO runner_jobs "
            "(id,project_id,actor_id,run_id,stage,status,source_job_id,receipt_sha256,commit,"
            "diff_sha256,tests) VALUES "
            "(:id,:project_id,'operator',:run_id,'qa','done',:engineer_id,:digest,:commit,"
            ":diff_sha256,CAST(:tests AS jsonb))"
        ),
        {
            "id": qa_id,
            "project_id": project_id,
            "run_id": run_id,
            "engineer_id": engineer_id,
            "digest": "c" * 64,
            "commit": "d" * 40,
            "diff_sha256": "e" * 64,
            "tests": json.dumps([{"name": "qa_fixture", "status": "passed", "exit_code": 0}]),
        },
    )
    return run_id, qa_id


def test_source_lineage_review_and_retrieval(client: TestClient, settings: Settings) -> None:
    project_id, profiles = seed_project(client)
    other_id, _ = seed_project(client)
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            sales_run, draft_id = seed_sales(connection, project_id, profiles, delivery_status="delivered")
            pending_run, _ = seed_sales(connection, project_id, profiles, delivery_status="pending")
            rejected_run, _ = seed_sales(connection, project_id, profiles, delivery_status="rejected")
            qa_run, qa_id = seed_qa(connection, project_id, profiles)
        root = f"/api/projects/{project_id}"
        pending = post(client, f"{root}/runs/{pending_run}/synthetic-memory-candidate", {"scope": "project"})
        assert pending.status_code == 409
        rejected = post(
            client, f"{root}/runs/{rejected_run}/synthetic-memory-candidate", {"scope": "project"}
        )
        assert rejected.status_code == 409
        assert client.get(f"{root}/candidates").json() == []
        sales = post(client, f"{root}/runs/{sales_run}/synthetic-memory-candidate", {"scope": "company"})
        qa = post(client, f"{root}/runs/{qa_run}/synthetic-memory-candidate", {"scope": "project"})
        assert sales.status_code == qa.status_code == 201
        sales_rows, qa_rows = sales.json(), qa.json()
        assert sales_rows["result"]["source_sales_draft_id"] == str(draft_id)
        assert qa_rows["result"]["source_runner_job_id"] == str(qa_id)
        assert sales_rows["candidate"]["status"] == qa_rows["candidate"]["status"] == "pending"
        assert client.get(f"{root}/memory?query=copper").json() == []
        repeat = post(client, f"{root}/runs/{sales_run}/synthetic-memory-candidate", {"scope": "company"})
        assert repeat.json() == sales_rows
        wrong_scope = post(
            client, f"{root}/runs/{sales_run}/synthetic-memory-candidate", {"scope": "project"}
        )
        assert wrong_scope.status_code == 409
        assert (
            post(
                client,
                f"/api/projects/{other_id}/runs/{qa_run}/synthetic-memory-candidate",
                {"scope": "company"},
            )
        ).status_code == 404
        for rows in (sales_rows, qa_rows):
            reviewed = post(
                client, f"{root}/candidates/{rows['candidate']['id']}/review", {"decision": "approved"}
            )
            assert reviewed.status_code == 200
            assert (
                post(
                    client,
                    f"{root}/candidates/{rows['candidate']['id']}/review",
                    {"decision": "rejected"},
                )
            ).status_code == 409
        company = client.get(f"/api/projects/{other_id}/memory?query=copper").json()
        local = client.get(f"{root}/memory?query=qa_fixture").json()
        assert len(company) == len(local) == 1
        assert company[0]["source_result_id"] == sales_rows["result"]["id"]
        assert company[0]["source_candidate_id"] == sales_rows["candidate"]["id"]
        assert local[0]["source_result_id"] == qa_rows["result"]["id"]
        assert local[0]["source_candidate_id"] == qa_rows["candidate"]["id"]
        assert client.get(f"/api/projects/{other_id}/memory?query=qa_fixture").json() == []
    finally:
        engine.dispose()
