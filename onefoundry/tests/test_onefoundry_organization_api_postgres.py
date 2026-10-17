"""Saved organization evidence and operator decisions on disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def _client(tmp_path: Path, mode: Literal["off", "records"] = "records") -> TestClient:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700, parents=True)
    input_root = tmp_path / "inputs"
    input_root.mkdir(mode=0o700)
    return TestClient(
        create_app(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
                app_public_port=8765,
                model_secret_root=str(secret_root),
                input_file_root=str(input_root),
                onefoundry_business_mode=mode,
            )
        ),
        base_url="http://localhost:8765",
    )


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


def _org(client: TestClient, project_id: str, name: str = "Fictional Grove District") -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/organizations", json={"display_name": name}, headers=ORIGIN
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _source(client: TestClient, project_id: str, org_id: str, revision: int = 1) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/organizations/{org_id}/sources",
        json={
            "source_kind": "public_excerpt",
            "official_url": "https://example.org/district/about",
            "excerpt": "Fictional Grove District serves four schools.",
            "observed_at": datetime.now(UTC).isoformat(),
            "expected_workflow_revision": revision,
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def test_off_suppresses_routes_and_records_persist(tmp_path: Path) -> None:
    with _client(tmp_path / "off", "off") as off:
        project_id = _project(off, "Off project")
        assert off.get("/api/onefoundry/business-mode").json() == {"mode": "off"}
        assert off.get(f"/api/projects/{project_id}/organizations").status_code == 404
        assert off.get(f"/api/projects/{project_id}/profiles").status_code == 200
    with _client(tmp_path / "on") as on:
        assert on.get("/api/onefoundry/business-mode").json() == {"mode": "records"}
        assert on.get(f"/api/projects/{project_id}/organizations").json() == []
        created = _org(on, project_id)
        detail = on.get(f"/api/projects/{project_id}/organizations/{created['id']}").json()
        assert detail["next_action"]["action_key"] == "add_source"
    with _client(tmp_path / "fallback", "off") as off:
        assert off.get(f"/api/projects/{project_id}/profiles").status_code == 200
    with _client(tmp_path / "reload") as on:
        assert on.get(f"/api/projects/{project_id}/organizations").json()[0]["id"] == created["id"]


def test_source_claim_review_qualification_and_event_redaction(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project(client, "Evidence project")
        org = _org(client, project_id)
        base = f"/api/projects/{project_id}/organizations/{org['id']}"
        bare = client.post(
            f"{base}/sources",
            json={
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/about",
                "observed_at": datetime.now(UTC).isoformat(),
                "expected_workflow_revision": 1,
            },
            headers=ORIGIN,
        )
        assert bare.status_code == 422
        bad_secret = client.post(
            f"{base}/sources",
            json={
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/about",
                "excerpt": "password=unsafe-value",
                "observed_at": datetime.now(UTC).isoformat(),
                "expected_workflow_revision": 1,
            },
            headers=ORIGIN,
        )
        assert bad_secret.status_code == 422
        secret_url = client.post(
            f"{base}/sources",
            json={
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/about?api_key=private-value",
                "excerpt": "Observed text",
                "observed_at": datetime.now(UTC).isoformat(),
                "expected_workflow_revision": 1,
            },
            headers=ORIGIN,
        )
        assert secret_url.status_code == 422
        source = _source(client, project_id, org["id"])
        assert source["digest"] and len(source["digest"]) == 64
        stale_source = client.post(
            f"{base}/sources",
            json={
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/about",
                "excerpt": "Other evidence",
                "observed_at": datetime.now(UTC).isoformat(),
                "expected_workflow_revision": 1,
            },
            headers=ORIGIN,
        )
        assert stale_source.status_code == 409
        proposed = client.post(
            f"{base}/claims",
            json={
                "source_id": source["id"],
                "claim_text": "Fictional Grove District serves four schools.",
                "locator": "About page, paragraph 1",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        )
        assert proposed.status_code == 201, proposed.text
        claim = proposed.json()
        assert claim["fresh"] and claim["review_state"] == "candidate"
        unsupported = client.post(
            f"{base}/claims",
            json={
                "source_id": source["id"],
                "claim_text": "A made-up unsupported fact.",
                "locator": "About page",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        )
        assert unsupported.status_code == 409
        review_url = f"/api/projects/{project_id}/claims/{claim['id']}/review"
        approved = client.post(
            review_url, json={"decision": "approved", "expected_revision": 1}, headers=ORIGIN
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["review_state"] == "approved"
        assert (
            client.post(
                review_url, json={"decision": "rejected", "expected_revision": 1}, headers=ORIGIN
            ).status_code
            == 409
        )
        detail = client.get(base).json()
        assert detail["next_action"]["action_key"] == "qualify"
        assert detail["workflow_status"] == "research_reviewed"
        qualified = client.post(
            f"{base}/qualification",
            json={"qualification": "qualified", "expected_revision": org["revision"]},
            headers=ORIGIN,
        )
        assert qualified.status_code == 200, qualified.text
        assert qualified.json()["qualification"] == "qualified"
        assert (
            client.post(
                f"{base}/qualification",
                json={"qualification": "declined", "expected_revision": org["revision"]},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        engine = create_engine(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
            ).database_url
        )
        with engine.connect() as connection:
            payloads: list[str] = list(
                map(
                    str,
                    connection.execute(
                        text(
                            "SELECT payload::text FROM events WHERE project_id=:project_id "
                            "AND event_type LIKE 'onefoundry_%'"
                        ),
                        {"project_id": project_id},
                    )
                    .scalars()
                    .all(),
                )
            )
        assert payloads and all("serves four schools" not in payload for payload in payloads)


def test_scope_reviewed_identity_and_supersession(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = _project(client, "First scope")
        second = _project(client, "Second scope")
        created = client.post(
            f"/api/projects/{first}/organizations",
            json={"display_name": "Grove", "canonical_domain": "example.org", "identity_reviewed": True},
            headers=ORIGIN,
        )
        assert created.status_code == 201
        org_id = created.json()["id"]
        duplicate = client.post(
            f"/api/projects/{first}/organizations",
            json={"display_name": "Another", "canonical_domain": "example.org", "identity_reviewed": True},
            headers=ORIGIN,
        )
        assert duplicate.status_code == 409
        assert client.get(f"/api/projects/{second}/organizations/{org_id}").status_code == 404
        assert client.get(f"/api/projects/{first}/organizations/{uuid4()}").status_code == 404
        source = _source(client, first, org_id)
        foreign = client.post(
            f"/api/projects/{second}/organizations/{org_id}/claims",
            json={
                "source_id": source["id"],
                "claim_text": "Fictional Grove District serves four schools.",
                "locator": "p1",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        )
        assert foreign.status_code == 404
        base = f"/api/projects/{first}/organizations/{org_id}"
        proposed = client.post(
            f"{base}/claims",
            json={
                "source_id": source["id"],
                "claim_text": "Fictional Grove District serves four schools.",
                "locator": "p1",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        ).json()
        replacement = client.post(
            f"{base}/sources",
            json={
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/about",
                "excerpt": "Fictional Grove District serves five schools.",
                "observed_at": datetime.now(UTC).isoformat(),
                "expected_workflow_revision": 2,
                "supersedes_source_id": source["id"],
            },
            headers=ORIGIN,
        )
        assert replacement.status_code == 201, replacement.text
        stale = client.get(base).json()["claims"][0]
        assert not stale["fresh"]
        assert (
            client.post(
                f"/api/projects/{first}/claims/{proposed['id']}/review",
                json={"decision": "approved", "expected_revision": 1},
                headers=ORIGIN,
            ).status_code
            == 409
        )


def test_input_file_source_requires_ready_same_project_and_matching_chunk(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project(client, "Input evidence")
        foreign_id = _project(client, "Foreign input evidence")
        org_id = _org(client, project_id)["id"]
        foreign_org_id = _org(client, foreign_id)["id"]
        file_id = uuid4()
        chunk_id = uuid4()
        engine = create_engine(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
            ).database_url
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO input_files (id,project_id,actor_id,display_name,extension,mime_type,"
                    "relative_path,sha256,byte_size,status) VALUES (:id,:project_id,'operator','fixture.txt',"
                    "'.txt','text/plain',:relative_path,:digest,10,'pending')"
                ),
                {
                    "id": file_id,
                    "project_id": project_id,
                    "relative_path": f"{project_id}/{file_id}",
                    "digest": "a" * 64,
                },
            )
        path = f"/api/projects/{project_id}/organizations/{org_id}/sources"
        body = {
            "source_kind": "input_file",
            "input_file_id": str(file_id),
            "observed_at": datetime.now(UTC).isoformat(),
            "expected_workflow_revision": 1,
        }
        assert client.post(path, json=body, headers=ORIGIN).status_code == 409
        assert (
            client.post(path, json={**body, "input_file_id": str(uuid4())}, headers=ORIGIN).status_code == 404
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE input_files SET status='ready_text',finished_at=now(),"
                    "extractor_version='fixture' "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": file_id},
            )
            connection.execute(
                text(
                    "INSERT INTO input_file_chunks "
                    "(id,project_id,actor_id,file_id,ordinal,source_label,content,sha256) "
                    "VALUES (:id,:project_id,'operator',:file_id,0,'Page 1',"
                    "'District enrollment is 42.',:digest)"
                ),
                {"id": chunk_id, "project_id": project_id, "file_id": file_id, "digest": "b" * 64},
            )
        saved = client.post(path, json=body, headers=ORIGIN)
        assert saved.status_code == 201, saved.text
        assert saved.json()["digest"] == "a" * 64
        claim_path = f"/api/projects/{project_id}/organizations/{org_id}/claims"
        unsupported = client.post(
            claim_path,
            json={
                "source_id": saved.json()["id"],
                "claim_text": "District enrollment is 43.",
                "locator": "Page 1",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        )
        assert unsupported.status_code == 409
        supported = client.post(
            claim_path,
            json={
                "source_id": saved.json()["id"],
                "claim_text": "District enrollment is 42.",
                "locator": "Page 1",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        )
        assert supported.status_code == 201, supported.text
        assert (
            client.post(
                f"/api/projects/{foreign_id}/organizations/{org_id}/sources", json=body, headers=ORIGIN
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/projects/{foreign_id}/organizations/{foreign_org_id}/sources",
                json=body,
                headers=ORIGIN,
            ).status_code
            == 404
        )


def test_concurrent_review_admits_one_decision(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id = _project(client, "Concurrent review")
        org_id = _org(client, project_id)["id"]
        source_id = _source(client, project_id, org_id)["id"]
        proposed = client.post(
            f"/api/projects/{project_id}/organizations/{org_id}/claims",
            json={
                "source_id": source_id,
                "claim_text": "Fictional Grove District serves four schools.",
                "locator": "p1",
                "expected_source_revision": 1,
            },
            headers=ORIGIN,
        )
        assert proposed.status_code == 201, proposed.text
        path = f"/api/projects/{project_id}/claims/{proposed.json()['id']}/review"

        def decide(decision: str) -> int:
            return client.post(
                path, json={"decision": decision, "expected_revision": 1}, headers=ORIGIN
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(decide, ("approved", "rejected")))
        assert sorted(statuses) == [200, 409]
        saved = client.get(f"/api/projects/{project_id}/organizations/{org_id}").json()
        assert saved["claims"][0]["revision"] == 2
