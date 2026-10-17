"""Atomic per-workflow limits and unknown usage on disposable PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from onefoundry.gateway_errors import Conflict
from onefoundry.inference import TextResult
from onefoundry.onefoundry_budget import context_limit, reserve, settle
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_live_text_postgres import FakeTransport, _configured
from tests.test_onefoundry_context_postgres import client, post, setup, source_and_claim


def _gateway() -> ToolGateway:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    return ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)


def test_unlimited_prompt_policy_persists_as_null_without_changing_other_limits(tmp_path: Path) -> None:
    with client(tmp_path, onefoundry_max_context_bytes=None) as api:
        project, org, researcher, _ = setup(api)
        source_and_claim(api, project, org)
        path = f"/api/projects/{project}/organizations/{org}"
        created = post(
            api,
            path + "/research-orders",
            {
                "profile_id": researcher,
                "idempotency_key": str(uuid4()),
                "expected_revision": 2,
            },
        )
        assert created.status_code == 201, created.text
        policy = api.get(path).json()["budget"]
        assert policy["context_bytes_limit"] is None
        assert policy["effective_context_bytes_limit"] is None
        assert (
            policy["linked_orders_limit"],
            policy["model_attempts_limit"],
            policy["public_web_calls_limit"],
        ) == (2, 2, 0)
        gateway = _gateway()
        with gateway.engine.connect() as connection:
            saved = connection.execute(
                text(
                    "SELECT context_bytes_limit,output_tokens_limit,elapsed_seconds_limit "
                    "FROM onefoundry_budget_snapshots WHERE project_id=:p"
                ),
                {"p": UUID(project)},
            ).one()
        assert tuple(saved) == (None, 512, 600)
        gateway.engine.dispose()
    unlimited = Settings(db_password="test-only", onefoundry_max_context_bytes=None)
    finite = Settings(db_password="test-only", onefoundry_max_context_bytes=2048)
    assert context_limit(unlimited, {"context_bytes_limit": 16384}) is None
    assert context_limit(finite, {"context_bytes_limit": None}) == 2048
    assert context_limit(finite, {"context_bytes_limit": 1024}) == 1024


def test_exact_limit_duplicate_and_unknown(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        project_id, org_id, researcher, _ = setup(api)
        source_and_claim(api, project_id, org_id)
        linked = post(
            api,
            f"/api/projects/{project_id}/organizations/{org_id}/research-orders",
            {"profile_id": researcher, "idempotency_key": str(uuid4()), "expected_revision": 2},
        )
        assert linked.status_code == 201, linked.text
        gateway = _gateway()
        with gateway.engine.connect() as connection:
            workflow_id = connection.scalar(
                text("SELECT id FROM onefoundry_workflows WHERE project_id=:p AND organization_id=:o"),
                {"p": UUID(project_id), "o": UUID(org_id)},
            )
        assert workflow_id is not None
        with gateway.engine.begin() as connection:
            first = reserve(
                connection, gateway, UUID(project_id), workflow_id, "model:first", "model_attempt"
            )
        with gateway.engine.begin() as connection:
            assert (
                reserve(connection, gateway, UUID(project_id), workflow_id, "model:first", "model_attempt")[
                    "id"
                ]
                == first["id"]
            )
            reserve(connection, gateway, UUID(project_id), workflow_id, "model:second", "model_attempt")
        with gateway.engine.begin() as connection, pytest.raises(Conflict, match="budget_exceeded"):
            reserve(connection, gateway, UUID(project_id), workflow_id, "model:third", "model_attempt")
        with gateway.engine.begin() as connection, pytest.raises(Conflict, match="budget_exceeded"):
            reserve(connection, gateway, UUID(project_id), workflow_id, "web:zero", "public_web_call")
        legacy = post(
            api,
            f"/api/projects/{project_id}/work-orders",
            {
                "title": "Unlinked",
                "instructions": "Plan only",
                "profile_id": researcher,
            },
        )
        assert legacy.status_code == 201
        order_id = legacy.json()["id"]
        assert post(api, f"/api/projects/{project_id}/work-orders/{order_id}/ready").status_code == 200
        run = post(api, f"/api/projects/{project_id}/work-orders/{order_id}/runs")
        assert run.status_code == 201, run.text
        with (
            gateway.engine.begin() as connection,
            pytest.raises(Conflict, match="not linked to this workflow"),
        ):
            settle(
                connection,
                gateway,
                UUID(project_id),
                first["id"],
                UUID(run.json()["id"]),
                "unknown",
                None,
                None,
            )
        linked_run = post(
            api,
            f"/api/projects/{project_id}/work-orders/{linked.json()['work_order_id']}/runs",
        )
        assert linked_run.status_code == 201, linked_run.text
        with gateway.engine.begin() as connection:
            settle(
                connection,
                gateway,
                UUID(project_id),
                first["id"],
                UUID(linked_run.json()["id"]),
                "unknown",
                None,
                None,
            )
        with gateway.engine.begin() as connection, pytest.raises(Conflict, match="usage_unknown"):
            reserve(connection, gateway, UUID(project_id), workflow_id, "web:first", "public_web_call")
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM onefoundry_workflows WHERE project_id=:p AND id=:w"),
                    {"p": UUID(project_id), "w": workflow_id},
                )
                == "unknown"
            )
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM onefoundry_budget_reservations "
                        "WHERE project_id=:p AND workflow_id=:w"
                    ),
                    {"p": UUID(project_id), "w": workflow_id},
                )
                == 3
            )
        gateway.engine.dispose()


def test_concurrent_reservation_never_exceeds_limit(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        project_id, org_id, _, _ = setup(api)
        gateway = _gateway()
        with gateway.engine.connect() as connection:
            workflow_id = connection.scalar(
                text("SELECT id FROM onefoundry_workflows WHERE project_id=:p AND organization_id=:o"),
                {"p": UUID(project_id), "o": UUID(org_id)},
            )
        assert workflow_id is not None

        def attempt(index: int) -> bool:
            try:
                with gateway.engine.begin() as connection:
                    reserve(
                        connection, gateway, UUID(project_id), workflow_id, f"model:{index}", "model_attempt"
                    )
                return True
            except Conflict:
                return False

        with ThreadPoolExecutor(max_workers=4) as executor:
            admitted = list(executor.map(attempt, range(4)))
        assert sum(admitted) == 2
        gateway.engine.dispose()


def test_budget_setting_bounds_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEFOUNDRY_MAX_MODEL_ATTEMPTS", "0")
    settings = Settings(db_password="local")
    assert settings.onefoundry_max_model_attempts == 0
    assert Settings(db_password="local", onefoundry_max_model_attempts=3).onefoundry_max_model_attempts == 3
    with pytest.raises(ValidationError):
        Settings(db_password="local", onefoundry_max_linked_orders=0)
    with pytest.raises(ValidationError):
        Settings(db_password="local", onefoundry_max_context_bytes=32001)
    with pytest.raises(ValidationError):
        Settings(
            db_password="local",
            onefoundry_business_mode="manual",
            onefoundry_max_output_tokens=513,
            live_text_max_output_tokens=512,
        )


def test_linked_live_run_settles_and_unknown_holds_capacity(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from onefoundry.main import create_app

    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
        model_secret_root=str(secret_root),
        onefoundry_business_mode="manual",
        onefoundry_max_model_attempts=1,
        onefoundry_max_output_tokens=32,
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as api:
        project_id, profile_id, _ = _configured(api)
        root = f"/api/projects/{project_id}"
        org = post(api, root + "/organizations", {"display_name": "Fictional Run District"})
        assert org.status_code == 201
        source_and_claim(api, project_id, org.json()["id"])
        order = post(
            api,
            root + f"/organizations/{org.json()['id']}/research-orders",
            {
                "profile_id": profile_id,
                "idempotency_key": str(uuid4()),
                "expected_revision": 2,
            },
        )
        assert order.status_code == 201, order.text
        fake = FakeTransport(TextResult("[]", "test-output", 4, 2, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            run = post(api, root + f"/work-orders/{order.json()['work_order_id']}/live-text-run")
        assert run.status_code == 201, run.text
        assert fake.calls[0].max_output_tokens == 32
        gateway = _gateway()
        with gateway.engine.connect() as connection:
            reservation = (
                connection.execute(
                    text(
                        "SELECT state,observed_tokens,observed_cost_usd FROM onefoundry_budget_reservations "
                        "WHERE project_id=:p AND operation_key=:key"
                    ),
                    {"p": UUID(project_id), "key": f"model_attempt:{run.json()['run']['id']}"},
                )
                .mappings()
                .one()
            )
            assert reservation["state"] == "settled"
            assert reservation["observed_tokens"] == 6
            assert reservation["observed_cost_usd"] is None
        gateway.engine.dispose()

    unknown_root = tmp_path / "unknown"
    unknown_root.mkdir()
    secret_root = unknown_root / "secrets"
    secret_root.mkdir(mode=0o700)
    unknown_settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
        model_secret_root=str(secret_root),
        onefoundry_business_mode="manual",
        onefoundry_max_model_attempts=1,
    )
    with TestClient(create_app(unknown_settings), base_url="http://localhost:8765") as api:
        project_id, profile_id, _ = _configured(api)
        root = f"/api/projects/{project_id}"
        org = post(api, root + "/organizations", {"display_name": "Fictional Unknown District"}).json()
        source_and_claim(api, project_id, org["id"])
        order = post(
            api,
            root + f"/organizations/{org['id']}/research-orders",
            {
                "profile_id": profile_id,
                "idempotency_key": str(uuid4()),
                "expected_revision": 2,
            },
        ).json()
        with patch(
            "onefoundry.live_text_store._provider_transport",
            return_value=FakeTransport(RuntimeError("timeout")),
        ):
            run = post(api, root + f"/work-orders/{order['work_order_id']}/live-text-run")
        assert run.status_code == 503
        detail = api.get(root + f"/organizations/{org['id']}").json()
        assert detail["linked_order"]["status"] == "unknown"
        assert any(item["state"] == "unknown" for item in detail["budget"]["reservations"])
        retry = post(
            api,
            root + f"/work-orders/{order['work_order_id']}/live-text-restart",
            {
                "prior_run_id": run.json()["run_id"],
                "expected_version": 1,
                "acknowledge_unknown": True,
            },
        )
        assert retry.status_code == 409
