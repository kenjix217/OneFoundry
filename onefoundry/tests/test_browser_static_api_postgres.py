"""Mounted operator API for one approved static browser fixture and job receipt."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from runner.browser_static_supervisor import StaticBrowserReceipt
from tests.test_browser_static_fixtures_postgres import proposal
from tests.test_role_tool_admission_postgres import _run


def test_mounted_browser_fixture_approval_and_job_readback(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    spool_root = tmp_path / "runner-spool"
    spool_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
        model_secret_root=str(secrets),
        runner_spool_root=str(spool_root),
        browser_static_enabled=True,
        browser_static_image_id="sha256:" + "a" * 64,
    )
    engine = create_engine(settings.database_url)
    app = create_app(settings, engine=engine)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(app, base_url="http://localhost:8765") as client:
            project_id, run_id, profile_id = _run(client, gateway, tools=["browser_static_inspect_v1"])
            fixtures_url = f"/api/projects/{project_id}/runs/{run_id}/browser-static-fixtures"
            response = client.post(
                fixtures_url,
                json=proposal().model_dump(mode="json"),
                headers={"origin": "http://localhost:8765"},
            )
            assert response.status_code == 201, response.text
            pending = response.json()
            assert pending["status"] == "pending"
            assert "Approved page" not in response.text
            gateway.decide_approval(
                project_id,
                UUID(pending["approval_id"]),
                ApprovalDecision(decision="approved"),
            )
            approved = client.get(f"{fixtures_url}/{pending['id']}")
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"

            jobs = app.state.browser_static_jobs
            jobs_url = f"/api/projects/{project_id}/runs/{run_id}/browser-static-jobs"
            key = uuid4()
            created = client.post(
                jobs_url,
                json={"fixture_id": pending["id"], "idempotency_key": str(key)},
                headers={"origin": "http://localhost:8765"},
            )
            assert created.status_code == 201, created.text
            job = created.json()
            assert job["actor_id"] == "operator"
            by_key = client.get(f"{jobs_url}/by-key/{key}")
            assert by_key.status_code == 200
            assert by_key.json() == job
            assert (
                client.post(
                    jobs_url,
                    json={"fixture_id": pending["id"], "idempotency_key": str(key)},
                    headers={"origin": "http://localhost:8765"},
                ).json()["id"]
                == job["id"]
            )
            listed = client.get(jobs_url)
            assert listed.status_code == 200
            assert [row["id"] for row in listed.json()] == [str(job["id"])]
            assert "Approved page" not in listed.text
            revoke = client.patch(
                f"/api/projects/{project_id}/profiles/{profile_id}",
                json={"allowed_tools": []},
                headers={"origin": "http://localhost:8765"},
            )
            assert revoke.status_code == 200, revoke.text
            denied = client.post(
                jobs_url,
                json={"fixture_id": pending["id"], "idempotency_key": str(uuid4())},
                headers={"origin": "http://localhost:8765"},
            )
            assert denied.status_code == 409, denied.text
            queued = client.post(
                f"{jobs_url}/{job['id']}/reconcile",
                headers={"origin": "http://localhost:8765"},
            )
            assert queued.status_code == 409

            def inspect(target: object, *, html: bytes, css: bytes, image: str) -> StaticBrowserReceipt:
                assert html and css
                assert image == settings.browser_static_image_id
                owner = jobs.fixtures.approved(project_id, run_id, UUID(pending["id"]))
                assert target == owner.target
                return StaticBrowserReceipt(
                    binding_digest=owner.target.binding_digest,
                    source_sha256=hashlib.sha256(html + b"\x00" + css).hexdigest(),
                    title="Checked fixture",
                    text="Approved page",
                    final_url=owner.target.approved_url,
                    blocked_request_count=0,
                    image_id=image,
                )

            jobs.spool.process_job(UUID(job["id"]), inspect=inspect)
            done = client.post(
                f"{jobs_url}/{job['id']}/reconcile",
                headers={"origin": "http://localhost:8765"},
            )
            assert done.status_code == 200, done.text
            assert done.json()["status"] == "done"
            assert "Approved page" not in done.text
            evidence = client.get(f"{jobs_url}/{job['id']}/evidence")
            assert evidence.status_code == 200
            assert evidence.json()["text"] == "Approved page"
            assert client.get(f"/api/projects/{UUID(int=0)}/runs/{run_id}/browser-static-jobs").json() == []
    finally:
        engine.dispose()
