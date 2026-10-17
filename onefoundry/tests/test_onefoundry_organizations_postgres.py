"""Additive schema and same-project relationships on PostgreSQL."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from onefoundry.main import create_app
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def test_schema_is_additive_and_rejects_foreign_workflow(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
        model_secret_root=str(secrets),
        input_file_root=str(inputs),
        onefoundry_business_mode="records",
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        first = client.post("/api/projects", json={"name": "Schema first"}, headers=ORIGIN).json()["id"]
        second = client.post("/api/projects", json={"name": "Schema second"}, headers=ORIGIN).json()["id"]
        org = client.post(
            f"/api/projects/{first}/organizations", json={"display_name": "Fixture District"}, headers=ORIGIN
        ).json()["id"]
        profiles = client.get(f"/api/projects/{first}/profiles").json()
        old_order = client.post(
            f"/api/projects/{first}/work-orders",
            json={
                "title": "Legacy order",
                "instructions": "Keep the prior contract",
                "profile_id": profiles[0]["id"],
            },
            headers=ORIGIN,
        )
        assert old_order.status_code == 201, old_order.text
        assert client.get(f"/api/projects/{first}/work-orders").json()[0]["id"] == old_order.json()["id"]
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        workflow = connection.scalar(
            text("SELECT id FROM onefoundry_workflows WHERE project_id=:id"), {"id": first}
        )
        assert workflow is not None
        assert (
            connection.scalar(
                text("SELECT count(*) FROM onefoundry_work_order_subjects WHERE project_id=:id"),
                {"id": first},
            )
            == 0
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO onefoundry_workflows (id,project_id,actor_id,organization_id) "
                "VALUES (:id,:project_id,'operator',:organization_id)"
            ),
            {"id": uuid4(), "project_id": second, "organization_id": org},
        )
