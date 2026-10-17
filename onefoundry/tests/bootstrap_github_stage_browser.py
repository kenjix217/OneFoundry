"""Seed a disposable browser acceptance project without contacting GitHub."""

import argparse
import json
import re
import secrets
from typing import Any, cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from onefoundry.main import create_app
from onefoundry.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--owner", default="sample")
    parser.add_argument("--repo", default="project")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.revision) is None:
        parser.error("revision must be an exact lowercase commit SHA")
    settings = Settings()
    base_url = f"http://localhost:{settings.app_public_port}"
    with TestClient(create_app(settings), base_url=base_url) as client:

        def post(path: str, payload: dict[str, object] | None = None) -> dict[str, Any]:
            response = client.post(path, json=payload, headers={"origin": base_url})
            if response.status_code not in (200, 201):
                raise RuntimeError(f"fixture setup failed at {path}: HTTP {response.status_code}")
            return cast(dict[str, Any], response.json())

        project = post("/api/projects", {"name": "Offline synthetic GitHub stage"})
        project_id = str(project["id"])
        base = f"/api/projects/{project_id}"
        profiles = client.get(f"{base}/profiles").json()
        engineer = next(item for item in profiles if item["name"] == "Engineer")
        order = post(
            f"{base}/work-orders",
            {
                "title": "Synthetic pinned source stage",
                "instructions": "Stage the local synthetic fixture",
                "profile_id": engineer["id"],
            },
        )
        post(f"{base}/work-orders/{order['id']}/ready")
        run = post(f"{base}/work-orders/{order['id']}/runs")
        account = post(
            f"{base}/github/accounts",
            {
                "label": "Synthetic acceptance only",
                "token": secrets.token_urlsafe(32),
            },
        )
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", args.revision)):
            source = post(
                f"{base}/sources/github",
                {
                    "account_id": account["id"],
                    "owner": args.owner,
                    "repo": args.repo,
                    "branch": "main",
                },
            )
        proposal = post(
            f"{base}/tools/github/stage-proposals",
            {
                "run_id": run["id"],
                "source_id": source["id"],
            },
        )
        approval_id = proposal["approval"]["id"]
        print(
            json.dumps(
                {
                    "project_id": project_id,
                    "approval_id": approval_id,
                    "source_id": source["id"],
                    "revision": args.revision,
                }
            )
        )


if __name__ == "__main__":
    main()
