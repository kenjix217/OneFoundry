"""Exact procedure pinning and isolation on migrated PostgreSQL."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.inference import TextRequest, TextResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def _project_profile(client: TestClient, name: str) -> tuple[str, str]:
    project_id = client.post("/api/projects", json={"name": name}, headers=ORIGIN).json()["id"]
    profile_id = client.get(f"/api/projects/{project_id}/profiles").json()[0]["id"]
    return project_id, profile_id


def _version(client: TestClient, project_id: str, skill_id: str, content: str) -> dict[str, object]:
    response = client.post(
        f"/api/projects/{project_id}/skills/{skill_id}/versions",
        json={"content": content},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _order_run(client: TestClient, project_id: str, profile_id: str) -> str:
    base = f"/api/projects/{project_id}/work-orders"
    order = client.post(
        base,
        json={"title": "Draft", "instructions": "Prepare copy", "profile_id": profile_id},
        headers=ORIGIN,
    )
    assert order.status_code == 201
    order_id = order.json()["id"]
    assert client.post(f"{base}/{order_id}/ready", headers=ORIGIN).status_code == 200
    run = client.post(f"{base}/{order_id}/runs", headers=ORIGIN)
    assert run.status_code == 201, run.text
    return str(run.json()["id"])


def test_skill_rename_preserves_id_versions_and_pins(client: TestClient) -> None:
    project_id, profile_id = _project_profile(client, "Rename procedures")
    other_id, _ = _project_profile(client, "Other rename project")
    base = f"/api/projects/{project_id}"
    created = client.post(
        f"{base}/skills",
        json={"name": "AgentPod review", "description": "[AgentPod pack] Review work."},
        headers=ORIGIN,
    ).json()
    skill_id = created["id"]
    version = _version(client, project_id, skill_id, "# AgentPod review\nRead the saved result.")
    binding_path = f"{base}/profiles/{profile_id}/skills/{skill_id}"
    binding = client.put(
        binding_path, json={"version": 1, "expected_binding_id": None}, headers=ORIGIN
    ).json()
    rename_path = f"{base}/skills/{skill_id}"
    payload = {
        "expected_name": created["name"],
        "expected_description": created["description"],
        "name": "OneFoundry review",
        "description": "[OneFoundry pack] Review work.",
    }
    assert client.patch(rename_path, json={"name": ""}, headers=ORIGIN).status_code == 422
    assert (
        client.patch(f"/api/projects/{other_id}/skills/{skill_id}", json=payload, headers=ORIGIN).status_code
        == 404
    )
    renamed = client.patch(rename_path, json=payload, headers=ORIGIN)
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["id"], renamed.json()["name"], renamed.json()["latest_version"]) == (
        skill_id,
        "OneFoundry review",
        1,
    )
    assert client.patch(rename_path, json=payload, headers=ORIGIN).status_code == 200
    assert (
        client.patch(
            rename_path,
            json={**payload, "name": "Another name"},
            headers=ORIGIN,
        ).status_code
        == 409
    )
    assert client.get(f"{base}/skills/{skill_id}/versions/1").json() == version
    pinned = client.get(f"{base}/profiles/{profile_id}/skills").json()[0]
    assert (pinned["id"], pinned["skill_name"], pinned["version"]) == (binding["id"], "OneFoundry review", 1)
    bootstrap = Settings(db_user=os.environ["DB_USER"], db_password=os.environ["DB_PASSWORD"])
    engine = create_engine(bootstrap.database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:project_id AND event_type='skill_renamed'"
                ),
                {"project_id": UUID(project_id)},
            )
            == 1
        )
    engine.dispose()


def test_manual_run_does_not_claim_unprompted_skill_was_used(client: TestClient) -> None:
    project_id, profile_id = _project_profile(client, "Automatic procedures")
    base = f"/api/projects/{project_id}"
    selected_id = client.post(
        f"{base}/skills",
        json={"name": "Market sizing", "description": "Estimate market size for a product opportunity"},
        headers=ORIGIN,
    ).json()["id"]
    unrelated_id = client.post(
        f"{base}/skills",
        json={"name": "Security patch", "description": "Review a software vulnerability patch"},
        headers=ORIGIN,
    ).json()["id"]
    _version(client, project_id, selected_id, "# Market sizing\nEstimate the addressable market.")
    _version(client, project_id, unrelated_id, "# Security patch\nTrace the vulnerability.")
    assert client.get(f"{base}/profiles/{profile_id}/skills").json() == []

    order = client.post(
        f"{base}/work-orders",
        json={
            "title": "Market sizing for new product",
            "instructions": "Estimate the addressable market size and assumptions",
            "profile_id": profile_id,
        },
        headers=ORIGIN,
    ).json()
    assert client.post(f"{base}/work-orders/{order['id']}/ready", headers=ORIGIN).status_code == 200
    started = client.post(f"{base}/work-orders/{order['id']}/runs", headers=ORIGIN)
    assert started.status_code == 201, started.text
    snapshots = client.get(f"{base}/runs/{started.json()['id']}").json()["skill_snapshots"]
    assert snapshots == []


def test_skill_versions_bindings_snapshots_and_isolation(
    client: TestClient,
) -> None:
    project_id, profile_id = _project_profile(client, "Skill project")
    other_id, other_profile_id = _project_profile(client, "Other skill project")
    base = f"/api/projects/{project_id}"
    created = client.post(
        f"{base}/skills", json={"name": "Writing", "description": "Drafting procedure"}, headers=ORIGIN
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]
    assert created.json()["latest_version"] is None
    assert client.post(f"{base}/skills", json={"name": "Writing"}, headers=ORIGIN).status_code == 409
    first = _version(client, project_id, skill_id, "# Writing\nUse plain English.")
    second = _version(client, project_id, skill_id, "# Writing\nUse short sentences.")
    assert (first["version"], second["version"]) == (1, 2)
    assert first["sha256"] != second["sha256"]
    assert client.get(f"{base}/skills").json()[0]["latest_version"] == 2
    assert client.get(f"{base}/skills/{skill_id}/versions/1").json() == first
    assert [v["version"] for v in client.get(f"{base}/skills/{skill_id}/versions").json()] == [2, 1]
    binding_path = f"{base}/profiles/{profile_id}/skills/{skill_id}"
    bound = client.put(binding_path, json={"version": 1, "expected_binding_id": None}, headers=ORIGIN)
    assert bound.status_code == 200, bound.text
    assert bound.json()["version"] == 1 and bound.json()["sha256"] == first["sha256"]
    run_id = _order_run(client, project_id, profile_id)
    assert client.get(f"{base}/runs/{run_id}").json()["skill_snapshots"][0]["version"] == 1
    assert (
        client.put(binding_path, json={"version": 2, "expected_binding_id": None}, headers=ORIGIN).status_code
        == 409
    )
    rebound = client.put(
        binding_path, json={"version": 2, "expected_binding_id": bound.json()["id"]}, headers=ORIGIN
    )
    assert rebound.status_code == 200
    assert rebound.json()["id"] != bound.json()["id"]
    assert client.get(f"{base}/runs/{run_id}").json()["skill_snapshots"][0]["sha256"] == first["sha256"]
    next_run = _order_run(client, project_id, profile_id)
    assert client.get(f"{base}/runs/{next_run}").json()["skill_snapshots"][0]["sha256"] == second["sha256"]
    assert (
        client.delete(f"{binding_path}?expected_binding_id={bound.json()['id']}", headers=ORIGIN).status_code
        == 409
    )
    assert (
        client.delete(
            f"{binding_path}?expected_binding_id={rebound.json()['id']}", headers=ORIGIN
        ).status_code
        == 204
    )
    assert client.get(f"{base}/profiles/{profile_id}/skills").json() == []
    assert client.get(f"{base}/runs/{next_run}").json()["skill_snapshots"][0]["version"] == 2
    assert client.get(f"/api/projects/{other_id}/skills/{skill_id}/versions").status_code == 404
    assert client.get(f"/api/projects/{other_id}/profiles/{other_profile_id}/skills").json() == []
    assert (
        client.put(
            f"/api/projects/{other_id}/profiles/{other_profile_id}/skills/{skill_id}",
            json={"version": 1, "expected_binding_id": None},
            headers=ORIGIN,
        ).status_code
        == 404
    )
    bootstrap = Settings(db_user=os.environ["DB_USER"], db_password=os.environ["DB_PASSWORD"])
    engine = create_engine(bootstrap.database_url)
    with engine.begin() as connection:
        with pytest.raises(DBAPIError):
            with connection.begin_nested():
                connection.execute(
                    text("UPDATE skill_versions SET content='tampered' WHERE id=:id"),
                    {"id": UUID(str(first["id"]))},
                )
        with pytest.raises(DBAPIError):
            with connection.begin_nested():
                connection.execute(
                    text("DELETE FROM skill_versions WHERE id=:id"),
                    {"id": UUID(str(first["id"]))},
                )
        assert (
            connection.scalar(
                text("SELECT content FROM skill_versions WHERE id=:id"), {"id": UUID(str(first["id"]))}
            )
            == first["content"]
        )
    engine.dispose()


def test_skill_binding_rejects_stale_aba_writes(client: TestClient) -> None:
    project_id, profile_id = _project_profile(client, "Binding ABA")
    base = f"/api/projects/{project_id}"
    skill_id = client.post(f"{base}/skills", json={"name": "Review"}, headers=ORIGIN).json()["id"]
    _version(client, project_id, skill_id, "# Review v1")
    _version(client, project_id, skill_id, "# Review v2")
    path = f"{base}/profiles/{profile_id}/skills/{skill_id}"
    first = client.put(path, json={"version": 1, "expected_binding_id": None}, headers=ORIGIN)
    assert first.status_code == 200, first.text
    second = client.put(path, json={"version": 2, "expected_binding_id": first.json()["id"]}, headers=ORIGIN)
    assert second.status_code == 200, second.text
    restored = client.put(
        path, json={"version": 1, "expected_binding_id": second.json()["id"]}, headers=ORIGIN
    )
    assert restored.status_code == 200, restored.text
    assert len({first.json()["id"], second.json()["id"], restored.json()["id"]}) == 3
    assert (
        client.put(
            path, json={"version": 2, "expected_binding_id": first.json()["id"]}, headers=ORIGIN
        ).status_code
        == 409
    )
    assert (
        client.delete(f"{path}?expected_binding_id={first.json()['id']}", headers=ORIGIN).status_code == 409
    )
    assert client.get(f"{base}/profiles/{profile_id}/skills").json()[0]["id"] == restored.json()["id"]
    assert (
        client.delete(f"{path}?expected_binding_id={restored.json()['id']}", headers=ORIGIN).status_code
        == 204
    )
    recreated = client.put(path, json={"version": 1, "expected_binding_id": None}, headers=ORIGIN)
    assert recreated.status_code == 200
    assert recreated.json()["id"] != restored.json()["id"]
    assert (
        client.delete(f"{path}?expected_binding_id={restored.json()['id']}", headers=ORIGIN).status_code
        == 409
    )


def test_skill_boundary_rejects_secret_and_authority_fields(client: TestClient) -> None:
    project_id, profile_id = _project_profile(client, "Skill validation")
    base = f"/api/projects/{project_id}"
    for body in (
        {"name": " "},
        {"name": "Unsafe", "description": "token=supersecret"},
        {"name": "Authority", "allowed_tools": ["web_search"]},
    ):
        assert client.post(f"{base}/skills", json=body, headers=ORIGIN).status_code == 422
    skill_id = client.post(f"{base}/skills", json={"name": "Safe"}, headers=ORIGIN).json()["id"]
    for body in (
        {"content": "  "},
        {"content": "Use sk-abcdefghijklmnop"},
        {"content": "Read", "allowed_tools": ["web_search"]},
    ):
        assert client.post(f"{base}/skills/{skill_id}/versions", json=body, headers=ORIGIN).status_code == 422
    assert (
        client.put(
            f"{base}/profiles/{profile_id}/skills/{skill_id}",
            json={"version": 1, "expected_binding_id": None},
            headers=ORIGIN,
        ).status_code
        == 404
    )


def test_concurrent_versions_are_atomic(client: TestClient, settings: Settings) -> None:
    project_id, _profile_id = _project_profile(client, "Concurrent version")
    skill_id = client.post(
        f"/api/projects/{project_id}/skills", json={"name": "Procedure"}, headers=ORIGIN
    ).json()["id"]

    def add(index: int) -> int:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as worker:
            result = worker.post(
                f"/api/projects/{project_id}/skills/{skill_id}/versions",
                json={"content": f"Procedure {index}"},
                headers=ORIGIN,
            )
            assert result.status_code == 201, result.text
            return int(result.json()["version"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(add, range(4)))
    assert sorted(versions) == [1, 2, 3, 4]


class CaptureTransport:
    def __init__(self) -> None:
        self.calls: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.calls.append(request)
        return TextResult("Draft complete.", "skill-test", 10, 3, "completed")


def test_live_text_automatically_receives_relevant_unpinned_skill(
    client: TestClient, settings: Settings
) -> None:
    project_id, _ = _project_profile(client, "Automatic live skill")
    base = f"/api/projects/{project_id}"
    account = client.post(
        f"{base}/model/accounts",
        json={"provider": "openai", "label": "Test", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    ).json()
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
    ):
        assert (
            client.post(f"{base}/model/accounts/{account['id']}/refresh", headers=ORIGIN).status_code == 200
        )
    profile = client.post(
        f"{base}/profiles",
        json={
            "name": "Analyst",
            "instructions": "Give concise estimates.",
            "model": "gpt-test",
            "model_account_id": account["id"],
            "allowed_tools": [],
        },
        headers=ORIGIN,
    ).json()
    skill_id = client.post(
        f"{base}/skills",
        json={"name": "Market sizing", "description": "Estimate market size and assumptions"},
        headers=ORIGIN,
    ).json()["id"]
    latest = _version(client, project_id, skill_id, "# Market sizing\nState the estimate assumptions.")
    assert client.get(f"{base}/profiles/{profile['id']}/skills").json() == []
    order = client.post(
        f"{base}/work-orders",
        json={
            "title": "Market sizing",
            "instructions": "Estimate the market size for this product",
            "profile_id": profile["id"],
        },
        headers=ORIGIN,
    ).json()
    assert client.post(f"{base}/work-orders/{order['id']}/ready", headers=ORIGIN).status_code == 200
    transport = CaptureTransport()
    with patch("onefoundry.live_text_store._provider_transport", return_value=transport):
        response = client.post(f"{base}/work-orders/{order['id']}/live-text-run", headers=ORIGIN)
    assert response.status_code == 201, response.text
    assert str(latest["content"]) in transport.calls[0].prompt
    assert "grant no tools or permissions" in transport.calls[0].prompt
    detail = client.get(f"{base}/runs/{response.json()['run']['id']}").json()
    assert [(item["skill_id"], item["sha256"]) for item in detail["skill_snapshots"]] == [
        (skill_id, latest["sha256"])
    ]
    started = next(item for item in detail["events"] if item["event_type"] == "live_text_started")
    assert started["payload"]["skills"][0]["selection_source"] == "automatic"
    settings.skill_auto_max_count = 0
    disabled_order = client.post(
        f"{base}/work-orders",
        json={
            "title": "Market sizing again",
            "instructions": "Estimate market size",
            "profile_id": profile["id"],
        },
        headers=ORIGIN,
    ).json()
    assert client.post(f"{base}/work-orders/{disabled_order['id']}/ready", headers=ORIGIN).status_code == 200
    with patch("onefoundry.live_text_store._provider_transport", return_value=transport):
        disabled = client.post(f"{base}/work-orders/{disabled_order['id']}/live-text-run", headers=ORIGIN)
    assert disabled.status_code == 201, disabled.text
    assert str(latest["content"]) not in transport.calls[-1].prompt
    assert client.get(f"{base}/runs/{disabled.json()['run']['id']}").json()["skill_snapshots"] == []
    settings.skill_auto_max_count = 10
    settings.skill_auto_max_content_bytes = 48000
    settings.live_text_max_prompt_bytes = 4220
    for index in range(10):
        named = client.post(
            f"{base}/skills",
            json={"name": f"Market {index:02d} " + "界" * 110, "description": "Market estimates"},
            headers=ORIGIN,
        )
        assert named.status_code == 201, named.text
        _version(client, project_id, named.json()["id"], "# Market\n" + "x" * 191)
    bounded_order = client.post(
        f"{base}/work-orders",
        json={"title": "Market", "instructions": "Market estimate", "profile_id": profile["id"]},
        headers=ORIGIN,
    ).json()
    assert client.post(f"{base}/work-orders/{bounded_order['id']}/ready", headers=ORIGIN).status_code == 200
    with patch("onefoundry.live_text_store._provider_transport", return_value=transport):
        bounded = client.post(f"{base}/work-orders/{bounded_order['id']}/live-text-run", headers=ORIGIN)
    assert bounded.status_code == 201, bounded.text
    saved = client.get(f"{base}/runs/{bounded.json()['run']['id']}").json()["skill_snapshots"]
    assert 0 < len(saved) < 10
    assert len(transport.calls[-1].prompt.encode()) + len(b"Give concise estimates.") <= 4220


def test_live_text_uses_exact_bound_procedure_before_egress(
    client: TestClient,
    settings: Settings,
) -> None:
    project_id, _unused = _project_profile(client, "Live skill")
    base = f"/api/projects/{project_id}"
    account_path = f"{base}/model/accounts"
    account = client.post(
        account_path,
        json={"provider": "openai", "label": "Test", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    )
    assert account.status_code == 201
    account_id = account.json()["id"]
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
    ):
        assert client.post(f"{account_path}/{account_id}/refresh", headers=ORIGIN).status_code == 200
    profile = client.post(
        f"{base}/profiles",
        json={
            "name": "Writer",
            "instructions": "Write plain language.",
            "model": "gpt-test",
            "model_account_id": account_id,
            "allowed_tools": [],
        },
        headers=ORIGIN,
    )
    assert profile.status_code == 201, profile.text
    profile_id = profile.json()["id"]
    skill_id = client.post(f"{base}/skills", json={"name": "Copy style"}, headers=ORIGIN).json()["id"]
    version = _version(client, project_id, skill_id, "# Copy style\nUse one short paragraph.")
    assert (
        client.put(
            f"{base}/profiles/{profile_id}/skills/{skill_id}",
            json={"version": 1, "expected_binding_id": None},
            headers=ORIGIN,
        ).status_code
        == 200
    )
    order = client.post(
        f"{base}/work-orders",
        json={"title": "Launch", "instructions": "Draft a launch note", "profile_id": profile_id},
        headers=ORIGIN,
    ).json()
    assert client.post(f"{base}/work-orders/{order['id']}/ready", headers=ORIGIN).status_code == 200
    transport = CaptureTransport()
    with patch("onefoundry.live_text_store._provider_transport", return_value=transport):
        result = client.post(
            f"{base}/work-orders/{order['id']}/live-text-run",
            headers=ORIGIN,
        )
    assert result.status_code == 201, result.text
    assert len(transport.calls) == 1
    assert str(version["content"]) in transport.calls[0].prompt
    assert "grant no tools or permissions" in transport.calls[0].prompt
    run_id = result.json()["run"]["id"]
    detail = client.get(f"{base}/runs/{run_id}").json()
    assert detail["skill_snapshots"][0]["sha256"] == version["sha256"]
    started = next(e for e in detail["events"] if e["event_type"] == "live_text_started")
    assert started["payload"]["skills"] == [
        {"skill_id": skill_id, "version": 1, "sha256": version["sha256"], "selection_source": "pinned"}
    ]
    assert started["payload"]["tools_active"] is False
    assert detail["profile_snapshots"][0]["allowed_tools"] == []
    long_version = _version(client, project_id, skill_id, "x" * 2000)
    current_binding = client.get(f"{base}/profiles/{profile_id}/skills").json()[0]
    assert (
        client.put(
            f"{base}/profiles/{profile_id}/skills/{skill_id}",
            json={"version": long_version["version"], "expected_binding_id": current_binding["id"]},
            headers=ORIGIN,
        ).status_code
        == 200
    )
    settings.live_text_max_prompt_bytes = 1024
    next_order = client.post(
        f"{base}/work-orders",
        json={"title": "Too large", "instructions": "Draft", "profile_id": profile_id},
        headers=ORIGIN,
    ).json()
    assert client.post(f"{base}/work-orders/{next_order['id']}/ready", headers=ORIGIN).status_code == 200
    with patch("onefoundry.live_text_store._provider_transport", return_value=transport):
        rejected = client.post(f"{base}/work-orders/{next_order['id']}/live-text-run", headers=ORIGIN)
    assert rejected.status_code == 409
    assert len(transport.calls) == 1
    assert (
        next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == next_order["id"])[
            "status"
        ]
        == "ready"
    )
    assert client.get(f"{base}/runs").json()[0]["id"] == run_id


def test_skill_prompt_limit_blocks_run_and_egress(
    client: TestClient,
) -> None:
    project_id, profile_id = _project_profile(client, "Bounded skill")
    base = f"/api/projects/{project_id}"
    skill_id = client.post(f"{base}/skills", json={"name": "Long"}, headers=ORIGIN).json()["id"]
    _version(client, project_id, skill_id, "x" * 16000)
    assert (
        client.put(
            f"{base}/profiles/{profile_id}/skills/{skill_id}",
            json={"version": 1, "expected_binding_id": None},
            headers=ORIGIN,
        ).status_code
        == 200
    )
    # The same bound procedure participates in every run snapshot, even without live egress.
    run_id = _order_run(client, project_id, profile_id)
    assert client.get(f"{base}/runs/{run_id}").json()["skill_snapshots"][0]["version"] == 1
