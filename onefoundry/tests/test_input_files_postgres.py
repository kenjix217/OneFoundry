"""Input-file owner records require exact project ownership."""

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from onefoundry.input_file_bytes import InputFileBytes
from onefoundry.input_file_worker import process_one
from onefoundry.main import create_app
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def _settings(tmp_path: Path, *, link_limit: int = 8) -> Settings:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    input_root = tmp_path / "input-files"
    input_root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        input_file_root=str(input_root),
        input_file_max_bytes=1024,
        input_file_max_link_count=link_limit,
    )


def test_input_file_schema_rejects_cross_project_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Input owner"}, headers=ORIGIN).json()
        other = client.post("/api/projects", json={"name": "Other input owner"}, headers=ORIGIN).json()
    engine = create_engine(settings.database_url)
    file_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO input_files (id,project_id,actor_id,display_name,extension,mime_type,"
                "relative_path,sha256,byte_size,status,reason_code,finished_at) VALUES "
                "(:id,:project_id,'operator','notes.txt','txt','text/plain',:path,:sha,4,"
                "'rejected','test_fixture',now())"
            ),
            {
                "id": file_id,
                "project_id": project["id"],
                "path": f"{project['id']}/{file_id}",
                "sha": "a" * 64,
            },
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO input_file_links (id,project_id,actor_id,file_id,scope) "
                    "VALUES (:id,:project_id,'operator',:file_id,'project')"
                ),
                {"id": uuid4(), "project_id": other["id"], "file_id": file_id},
            )
    engine.dispose()


def test_upload_and_owner_readback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Attached project"}, headers=ORIGIN).json()
        other = client.post("/api/projects", json={"name": "Other attached project"}, headers=ORIGIN).json()
        path = f"/api/projects/{project['id']}/input-files"
        client_file_id = str(uuid4())
        response = client.post(
            path,
            content=b"Research first",
            headers={
                **ORIGIN,
                "X-File-Name": "brief.md",
                "X-Client-File-Id": client_file_id,
                "Content-Type": "application/octet-stream",
            },
        )
        assert response.status_code == 201, response.text
        saved = response.json()
        assert saved["id"] == client_file_id
        assert saved["display_name"] == "brief.md"
        assert saved["sha256"] == hashlib.sha256(b"Research first").hexdigest()
        assert saved["status"] == "pending"
        listed = client.get(path)
        assert listed.status_code == 200, listed.text
        assert listed.json()[0]["id"] == saved["id"]
        assert client.get(f"/api/projects/{other['id']}/input-files/{saved['id']}").status_code == 404
        engine = create_engine(settings.database_url)
        assert process_one(engine, InputFileBytes(settings.input_file_root), settings)
        assert client.get(f"{path}/{saved['id']}").json()["status"] == "ready_text"
        with engine.connect() as connection:
            chunk = connection.scalar(
                text(
                    "SELECT content FROM input_file_chunks WHERE project_id=:project_id AND file_id=:file_id"
                ),
                {"project_id": project["id"], "file_id": saved["id"]},
            )
        assert chunk == "Research first"
        engine.dispose()
        oversized = client.post(
            path,
            content=b"a" * (settings.input_file_max_bytes + 1),
            headers={**ORIGIN, "X-File-Name": "large.txt", "Content-Type": "application/octet-stream"},
        )
        assert oversized.status_code == 413


def test_work_order_file_count_is_enforced_by_api(tmp_path: Path) -> None:
    settings = _settings(tmp_path, link_limit=1)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Limited files"}, headers=ORIGIN).json()
        base = f"/api/projects/{project['id']}"
        profile = client.get(base + "/profiles").json()[0]
        order = client.post(
            base + "/work-orders",
            json={
                "title": "Read these",
                "instructions": "Use the attached note",
                "profile_id": profile["id"],
            },
            headers=ORIGIN,
        ).json()
        path = f"{base}/input-files?scope=work_order&work_order_id={order['id']}"
        headers = {**ORIGIN, "X-File-Name": "note.md", "Content-Type": "application/octet-stream"}
        first = client.post(path, content=b"first", headers=headers)
        assert first.status_code == 201, first.text
        second = client.post(path, content=b"second", headers=headers)
        assert second.status_code == 409
        listed = client.get(f"{base}/input-files?work_order_id={order['id']}").json()
        assert [item["id"] for item in listed] == [first.json()["id"]]
