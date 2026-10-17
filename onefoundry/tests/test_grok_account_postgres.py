"""Grok subscription login, model binding, and live text on migrated PostgreSQL."""

import os
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from onefoundry.grok_account import DeviceChallenge, GrokConnectionError, GrokConnectionPending, encode_state
from onefoundry.inference import TextResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def test_grok_subscription_connection_profile_and_text_run(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        xai_oauth_client_id="fixture-client",
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Grok pilot"}, headers=ORIGIN).json()["id"]
        base = f"/api/projects/{project}"
        model = f"{base}/model"
        provider = next(row for row in client.get(f"{model}/providers").json() if row["id"] == "xai")
        assert provider["connectable"] and provider["auth_kind"] == "account_runtime"
        assert provider["text_runtime_available"]
        assert (
            client.post(
                f"{model}/accounts",
                json={"provider": "xai", "label": "Wrong path", "api_key": "test-key"},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        challenge = DeviceChallenge(
            "https://auth.x.ai/device", "GROK-CODE", int(time.time()) + 600, 5, "private-device"
        )
        with patch("onefoundry.model_gateway.start_grok_device_login", return_value=challenge):
            started = client.post(f"{model}/grok/start", json={"label": "My Grok"}, headers=ORIGIN)
        assert started.status_code == 201, started.text
        account_id = started.json()["account"]["id"]
        assert "private-device" not in started.text
        assert (root / account_id).stat().st_mode & 0o777 == 0o600
        shown = client.get(f"{model}/accounts/{account_id}/grok/challenge")
        assert shown.status_code == 200 and shown.json()["user_code"] == "GROK-CODE"
        with patch("onefoundry.model_gateway.finish_grok_device_login", side_effect=GrokConnectionPending()):
            pending = client.post(f"{model}/accounts/{account_id}/grok/finish", headers=ORIGIN)
        assert pending.status_code == 409
        state = {
            "kind": "connected",
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires_at": int(time.time()) + 3600,
        }
        # A token file may survive a database interruption after one-time exchange.
        ModelSecretStore(str(root)).write(UUID(account_id), encode_state(state))
        with patch("onefoundry.model_gateway.finish_grok_device_login") as exchange:
            finished = client.post(f"{model}/accounts/{account_id}/grok/finish", headers=ORIGIN)
        exchange.assert_not_called()
        assert finished.status_code == 200 and finished.json()["credential_present"]
        assert "private-access" not in finished.text and "private-refresh" not in finished.text
        with patch("onefoundry.model_gateway.start_grok_device_login") as no_restart:
            assert (
                client.post(f"{model}/accounts/{account_id}/grok/restart", headers=ORIGIN).status_code == 409
            )
        no_restart.assert_not_called()
        with patch(
            "onefoundry.model_gateway.discover_grok_models",
            return_value=[DiscoveredModel("grok-test", "Grok Test", "text")],
        ) as discover:
            refreshed = client.post(f"{model}/accounts/{account_id}/refresh", headers=ORIGIN)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["models"][0]["available"]
        assert discover.call_args.args[0]["access_token"] == "private-access"
        profile = client.post(
            f"{base}/profiles",
            json={
                "name": "Grok Writer",
                "instructions": "Write plainly.",
                "model": "grok-test",
                "model_account_id": account_id,
            },
            headers=ORIGIN,
        )
        assert profile.status_code == 201, profile.text
        order = client.post(
            f"{base}/work-orders",
            json={
                "title": "Pilot note",
                "instructions": "Write a short note",
                "profile_id": profile.json()["id"],
            },
            headers=ORIGIN,
        )
        assert order.status_code == 201, order.text
        order_id = order.json()["id"]
        assert client.post(f"{base}/work-orders/{order_id}/ready", headers=ORIGIN).status_code == 200
        with patch(
            "onefoundry.inference.subscription_grok.GrokAccountTransport.generate",
            autospec=True,
            return_value=TextResult("Pilot note complete.", "resp-test", 10, 3, "completed"),
        ) as generate:
            run = client.post(f"{base}/work-orders/{order_id}/live-text-run", headers=ORIGIN)
        assert run.status_code == 201, run.text
        assert generate.call_count == 1
        assert generate.call_args.args[1].model == "grok-test"
        assert run.json()["output"]["text"] == "Pilot note complete."
        assert run.json()["run"]["tokens_used"] == 13
        assert "private-access" not in run.text

        # A missing token file must not leave the ready database row or its
        # saved catalog visible as an operable subscription connection.
        stored = ModelSecretStore(str(root))
        stored.remove(UUID(account_id))
        listed = client.get(f"{model}/accounts").json()
        assert not next(item for item in listed if item["id"] == account_id)["credential_present"]
        assert not next(item for item in listed if item["id"] == account_id)["catalog_valid"]
        assert not client.get(f"{model}/catalog").json()[0]["available"]
        stored.write(UUID(account_id), encode_state(state))

        reconnect = DeviceChallenge(
            "https://auth.x.ai/device", "FRESH-CODE", int(time.time()) + 600, 5, "fresh-device"
        )
        with patch("onefoundry.model_gateway.start_grok_device_login", return_value=reconnect):
            renewed = client.post(f"{model}/accounts/{account_id}/grok/reconnect", headers=ORIGIN)
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["account"]["id"] == account_id
        assert renewed.json()["user_code"] == "FRESH-CODE"
        assert "fresh-device" not in renewed.text
        assert not renewed.json()["account"]["credential_present"]
        assert not renewed.json()["account"]["catalog_valid"]
        assert profile.json()["id"] in [item["id"] for item in client.get(f"{base}/profiles").json()]
        saved_models = client.get(f"{model}/catalog").json()
        assert saved_models[0]["model_id"] == "grok-test" and not saved_models[0]["available"]


def test_grok_subscription_requires_configured_client(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        xai_oauth_client_id="",
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Grok setup"}, headers=ORIGIN).json()["id"]
        model = f"/api/projects/{project}/model"
        provider = next(row for row in client.get(f"{model}/providers").json() if row["id"] == "xai")
        assert not provider["connectable"]
        with patch("onefoundry.model_gateway.start_grok_device_login") as start:
            response = client.post(f"{model}/grok/start", json={"label": "Grok"}, headers=ORIGIN)
        assert response.status_code == 409
        start.assert_not_called()


def test_grok_expired_sign_in_restarts_on_same_account(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        xai_oauth_client_id="fixture-client",
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Grok retry"}, headers=ORIGIN).json()["id"]
        other = client.post("/api/projects", json={"name": "Other project"}, headers=ORIGIN).json()["id"]
        model = f"/api/projects/{project}/model"
        expired = DeviceChallenge(
            "https://auth.x.ai/device", "OLD-CODE", int(time.time()) - 1, 5, "old-device"
        )
        with patch("onefoundry.model_gateway.start_grok_device_login", return_value=expired):
            started = client.post(f"{model}/grok/start", json={"label": "Personal"}, headers=ORIGIN)
        assert started.status_code == 201, started.text
        account_id = started.json()["account"]["id"]
        assert client.get(f"{model}/accounts/{account_id}/grok/challenge").status_code == 409

        fresh = DeviceChallenge(
            "https://auth.x.ai/device", "NEW-CODE", int(time.time()) + 600, 5, "new-device"
        )
        with patch("onefoundry.model_gateway.start_grok_device_login", return_value=fresh) as device_start:
            assert (
                client.post(
                    f"/api/projects/{other}/model/accounts/{account_id}/grok/restart", headers=ORIGIN
                ).status_code
                == 404
            )
            device_start.assert_not_called()
            restarted = client.post(f"{model}/accounts/{account_id}/grok/restart", headers=ORIGIN)
        assert restarted.status_code == 200, restarted.text
        assert restarted.json()["account"]["id"] == account_id
        assert restarted.json()["user_code"] == "NEW-CODE"
        assert "new-device" not in restarted.text
        saved = ModelSecretStore(str(root)).read(UUID(account_id))
        assert "new-device" not in client.get(f"{model}/accounts/{account_id}/grok/challenge").text
        assert len(client.get(f"{model}/accounts").json()) == 1

        with patch(
            "onefoundry.model_gateway.start_grok_device_login", side_effect=GrokConnectionError("unavailable")
        ):
            failed = client.post(f"{model}/accounts/{account_id}/grok/restart", headers=ORIGIN)
        assert failed.status_code == 502
        assert ModelSecretStore(str(root)).read(UUID(account_id)) == saved
