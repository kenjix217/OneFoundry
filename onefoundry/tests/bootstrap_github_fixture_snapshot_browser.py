"""Seed a disposable v3 HTTPS fixture snapshot approval without contacting GitHub."""

import json
import secrets
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from onefoundry.main import create_app
from onefoundry.settings import Settings
from runner.github_https_fixture_server import COMMIT


def main() -> None:
    settings = Settings()
    base_url = f"http://localhost:{settings.app_public_port}"
    with TestClient(create_app(settings), base_url=base_url) as client:

        def post(path: str, payload: dict[str, object]) -> dict[str, Any]:
            response = client.post(path, json=payload, headers={"origin": base_url})
            if response.status_code not in (200, 201):
                raise RuntimeError(f"fixture setup failed at {path}: HTTP {response.status_code}")
            return cast(dict[str, Any], response.json())

        project = post("/api/projects", {"name": "Synthetic HTTPS GitHub snapshot"})
        project_id = str(project["id"])
        base = f"/api/projects/{project_id}"
        profiles = client.get(f"{base}/profiles").json()
        engineer = next(item for item in profiles if item["name"] == "Engineer")
        order = post(
            f"{base}/work-orders",
            {
                "title": "Snapshot synthetic HTTPS repository",
                "instructions": "Materialize a local fake HTTPS snapshot only; no coding launch",
                "profile_id": engineer["id"],
            },
        )
        post(f"{base}/work-orders/{order['id']}/ready", {})
        run = post(f"{base}/work-orders/{order['id']}/runs", {})
        account = post(
            f"{base}/github/accounts",
            {
                "label": "Synthetic acceptance account",
                "token": secrets.token_urlsafe(32),
            },
        )
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", COMMIT)):
            source = post(
                f"{base}/sources/github",
                {
                    "account_id": account["id"],
                    "owner": "sample",
                    "repo": "project",
                    "branch": "main",
                },
            )
        proposal = post(
            f"{base}/tools/github/fixture-snapshot-v3",
            {
                "run_id": run["id"],
                "source_id": source["id"],
                "idempotency_key": str(uuid4()),
            },
        )
        print(
            json.dumps(
                {
                    "project_id": project_id,
                    "approval_id": proposal["approval"]["id"],
                    "source_id": source["id"],
                    "revision": COMMIT,
                }
            )
        )


if __name__ == "__main__":
    main()
