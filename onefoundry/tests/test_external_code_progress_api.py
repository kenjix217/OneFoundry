"""HTTP progress contract preserves the scoped public summary."""

from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from onefoundry.external_code_api import make_external_code_router
from onefoundry.tool_gateway import ToolGateway
from runner.provider_progress import AGENT_UPDATE_TEXT


class _Gateway:
    def external_code_progress(
        self, project_id: UUID, run_id: UUID, attempt_id: UUID, after: int
    ) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "coverage": "partial",
            "latest_sequence": 1,
            "terminal": False,
            "items": []
            if after
            else [
                {
                    "sequence": 1,
                    "source": "cli_output",
                    "kind": "agent_update",
                    "text": AGENT_UPDATE_TEXT,
                    "tool": None,
                    "command": None,
                }
            ],
        }


def test_progress_route_returns_bounded_shape_and_validates_cursor() -> None:
    project, run, attempt = uuid4(), uuid4(), uuid4()
    app = FastAPI()
    app.include_router(make_external_code_router(cast(ToolGateway, _Gateway())))
    client = TestClient(app)
    route = f"/api/projects/{project}/runs/{run}/external-code/attempts/{attempt}/progress"
    first = client.get(route)
    assert first.status_code == 200
    assert first.json()["items"][0]["text"] == AGENT_UPDATE_TEXT
    assert client.get(route + "?after=1").json()["items"] == []
    assert client.get(route + "?after=25").status_code == 422
