"""Codex device account, model binding, and live text on migrated PostgreSQL."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from uuid import UUID

from fastapi.testclient import TestClient

from onefoundry.codex_account import (
    CodexConnectionPending,
    CodexDeviceStartError,
    CodexTokenExchangeError,
    CodexTokenExchangeUnknown,
    DeviceChallenge,
    decode_state,
    discover_codex_models,
    encode_state,
)
from onefoundry.inference import TextResult, TransportError
from onefoundry.inference.http import HTTPResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def test_codex_device_start_upstream_failure_saves_nothing(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Codex error"}, headers=ORIGIN).json()["id"]
        model = f"/api/projects/{project}/model"
        with patch("onefoundry.model_gateway.start_device_login", side_effect=CodexDeviceStartError(530)):
            failed = client.post(f"{model}/codex/start", json={"label": "Personal"}, headers=ORIGIN)
        assert failed.status_code == 502
        assert failed.json() == {"detail": "OpenAI sign-in endpoint returned HTTP 530; no connection saved"}
        assert client.get(f"{model}/accounts").json() == []
        assert [path for path in root.iterdir() if path.is_file()] == []


def test_expired_codex_sign_in_restarts_on_the_same_account(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Codex retry"}, headers=ORIGIN).json()["id"]
        other = client.post("/api/projects", json={"name": "Other project"}, headers=ORIGIN).json()["id"]
        model = f"/api/projects/{project}/model"
        expired = DeviceChallenge(
            "https://auth.openai.com/codex/device", "OLD-CODE", int(time.time()) - 1, 5, "old"
        )
        with patch("onefoundry.model_gateway.start_device_login", return_value=expired):
            started = client.post(f"{model}/codex/start", json={"label": "Personal"}, headers=ORIGIN)
        account_id = started.json()["account"]["id"]
        assert client.get(f"{model}/accounts/{account_id}/codex/challenge").status_code == 409

        fresh = DeviceChallenge(
            "https://auth.openai.com/codex/device", "NEW-CODE", int(time.time()) + 600, 5, "new"
        )
        with patch("onefoundry.model_gateway.start_device_login", return_value=fresh) as device_start:
            assert (
                client.post(
                    f"/api/projects/{other}/model/accounts/{account_id}/codex/restart", headers=ORIGIN
                ).status_code
                == 404
            )
            device_start.assert_not_called()
            restarted = client.post(f"{model}/accounts/{account_id}/codex/restart", headers=ORIGIN)
        assert restarted.status_code == 200, restarted.text
        assert restarted.json()["account"]["id"] == account_id
        assert restarted.json()["user_code"] == "NEW-CODE"
        assert '"device_code"' not in restarted.text and '"new"' not in restarted.text
        assert client.get(f"{model}/accounts/{account_id}/codex/challenge").json()["user_code"] == "NEW-CODE"
        assert len(client.get(f"{model}/accounts").json()) == 1

        with patch("onefoundry.model_gateway.finish_device_login", side_effect=CodexTokenExchangeError(400)):
            rejected = client.post(f"{model}/accounts/{account_id}/codex/finish", headers=ORIGIN)
        assert rejected.status_code == 502
        assert rejected.json() == {
            "detail": "OpenAI rejected the Codex token exchange (HTTP 400). "
            "Start fresh sign-in on this account."
        }

        with patch("onefoundry.model_gateway.finish_device_login", side_effect=CodexTokenExchangeUnknown()):
            uncertain = client.post(f"{model}/accounts/{account_id}/codex/finish", headers=ORIGIN)
        assert uncertain.status_code == 502
        assert uncertain.json() == {
            "detail": "OpenAI token exchange did not return a response. Its outcome is unknown; "
            "start fresh sign-in on this account."
        }

        prior = ModelSecretStore(str(root)).read(UUID(account_id))
        with patch("onefoundry.model_gateway.start_device_login", side_effect=CodexDeviceStartError(530)):
            failed = client.post(f"{model}/accounts/{account_id}/codex/restart", headers=ORIGIN)
        assert failed.status_code == 502
        assert ModelSecretStore(str(root)).read(UUID(account_id)) == prior

        connected = {
            "kind": "connected",
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "account_id": "private-account",
            "expires_at": int(time.time()) + 3600,
        }
        with patch("onefoundry.model_gateway.finish_device_login", return_value=connected) as exchange:
            assert (
                client.post(f"{model}/accounts/{account_id}/codex/finish", headers=ORIGIN).status_code == 200
            )
            assert (
                client.post(f"{model}/accounts/{account_id}/codex/finish", headers=ORIGIN).status_code == 200
            )
        exchange.assert_called_once()
        assert ModelSecretStore(str(root)).read(UUID(account_id)) == encode_state(connected)
        with patch("onefoundry.model_gateway.start_device_login") as device_start:
            assert (
                client.post(f"{model}/accounts/{account_id}/codex/restart", headers=ORIGIN).status_code == 409
            )
            device_start.assert_not_called()


def test_codex_finish_keeps_successful_one_time_exchange_when_account_parse_fails(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post(
            "/api/projects", json={"name": "Codex exchange recovery"}, headers=ORIGIN
        ).json()["id"]
        model = f"/api/projects/{project}/model"
        challenge = DeviceChallenge(
            "https://auth.openai.com/codex/device", "ABCD-EFGH", int(time.time()) + 600, 5, "private-device"
        )
        with patch("onefoundry.model_gateway.start_device_login", return_value=challenge):
            started = client.post(f"{model}/codex/start", json={"label": "Personal"}, headers=ORIGIN)
        account_id = UUID(started.json()["account"]["id"])
        endpoint = f"{model}/accounts/{account_id}/codex/finish"
        with (
            patch(
                "onefoundry.codex_account._post",
                return_value=(
                    200,
                    {"authorization_code": "private-code", "code_verifier": "private-verifier"},
                ),
            ) as device_poll,
            patch(
                "onefoundry.codex_account._token_exchange",
                return_value={"access_token": "private-access", "refresh_token": "private-refresh"},
            ) as token_exchange,
        ):
            rejected = client.post(endpoint, headers=ORIGIN)
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": "Codex login did not identify the account"}
        assert device_poll.call_count == token_exchange.call_count == 1
        saved = decode_state(ModelSecretStore(str(root)).read(account_id))
        assert saved["kind"] == "exchanged"
        assert saved["tokens"] == {"access_token": "private-access", "refresh_token": "private-refresh"}
        with patch("onefoundry.model_gateway.start_device_login") as new_challenge:
            restart = client.post(f"{model}/accounts/{account_id}/codex/restart", headers=ORIGIN)
        assert restart.status_code == 409
        assert new_challenge.call_count == 0
        assert decode_state(ModelSecretStore(str(root)).read(account_id)) == saved
        with patch("onefoundry.codex_account._account_id", return_value="private-account"):
            recovered = client.post(endpoint, headers=ORIGIN)
        assert recovered.status_code == 200
        assert recovered.json()["credential_present"] is True
        assert "private-access" not in recovered.text


def test_codex_connection_profile_and_text_run(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Codex pilot"}, headers=ORIGIN).json()["id"]
        base = f"/api/projects/{project}"
        model = f"{base}/model"
        provider = next(
            row for row in client.get(f"{model}/providers").json() if row["id"] == "openai_codex_runtime"
        )
        assert provider["connectable"] and provider["auth_kind"] == "account_runtime"
        assert provider["text_runtime_available"]
        assert (
            client.post(
                f"{model}/accounts",
                json={"provider": "openai_codex_runtime", "label": "Wrong path", "api_key": "test-key"},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        challenge = DeviceChallenge(
            "https://auth.openai.com/codex/device", "ABCD-EFGH", int(time.time()) + 600, 5, "private-device"
        )
        with patch("onefoundry.model_gateway.start_device_login", return_value=challenge):
            start = client.post(f"{model}/codex/start", json={"label": "My Codex"}, headers=ORIGIN)
        assert start.status_code == 201, start.text
        account_id = start.json()["account"]["id"]
        assert start.json()["user_code"] == "ABCD-EFGH"
        assert "private-device" not in start.text
        assert start.json()["account"]["credential_present"] is False
        assert (root / account_id).exists()
        assert (root / account_id).stat().st_mode & 0o777 == 0o600
        shown = client.get(f"{model}/accounts/{account_id}/codex/challenge")
        assert shown.status_code == 200 and shown.json()["user_code"] == "ABCD-EFGH"
        assert "private-device" not in shown.text
        with patch("onefoundry.model_gateway.finish_device_login", side_effect=CodexConnectionPending()):
            pending = client.post(f"{model}/accounts/{account_id}/codex/finish", headers=ORIGIN)
        assert pending.status_code == 409
        state = {
            "kind": "connected",
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "account_id": "private-account",
            "expires_at": None,
        }
        # A token file can survive a database failure. Finish must reconcile it
        # without repeating the one-time provider exchange.
        ModelSecretStore(str(root)).write(UUID(account_id), encode_state(state))
        with patch("onefoundry.model_gateway.finish_device_login") as exchange:
            finished = client.post(f"{model}/accounts/{account_id}/codex/finish", headers=ORIGIN)
        exchange.assert_not_called()
        assert finished.status_code == 200 and finished.json()["credential_present"]
        assert "private-access" not in finished.text and "private-refresh" not in finished.text
        with patch(
            "onefoundry.model_gateway.discover_codex_models",
            return_value=[DiscoveredModel("gpt-test", "GPT Test", "text", ("low", "high"))],
        ) as discover:
            refreshed = client.post(f"{model}/accounts/{account_id}/refresh", headers=ORIGIN)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["models"][0]["available"]
        assert refreshed.json()["models"][0]["reasoning_efforts"] == ["low", "high"]
        assert discover.call_args.args[0]["account_id"] == "private-account"
        profile = client.post(
            f"{base}/profiles",
            json={
                "name": "Codex Writer",
                "instructions": "Write plainly.",
                "model": "gpt-test",
                "model_account_id": account_id,
                "reasoning_effort": "high",
            },
            headers=ORIGIN,
        )
        assert profile.status_code == 201, profile.text
        assert profile.json()["reasoning_effort"] == "high"
        rejected = client.patch(
            f"{base}/profiles/{profile.json()['id']}",
            json={"reasoning_effort": "xhigh"},
            headers=ORIGIN,
        )
        assert rejected.status_code == 422
        order = client.post(
            f"{base}/work-orders",
            json={
                "title": "Pilot note",
                "instructions": "Write a short note",
                "profile_id": profile.json()["id"],
            },
            headers=ORIGIN,
        )
        assert order.status_code == 201
        order_id = order.json()["id"]
        assert client.post(f"{base}/work-orders/{order_id}/ready", headers=ORIGIN).status_code == 200

        with patch(
            "onefoundry.inference.subscription_codex.CodexAccountTransport.generate",
            autospec=True,
            return_value=TextResult("Pilot note complete.", "resp-test", 10, 3, "completed"),
        ) as generate:
            run = client.post(f"{base}/work-orders/{order_id}/live-text-run", headers=ORIGIN)
        assert run.status_code == 201, run.text
        assert generate.call_count == 1
        assert generate.call_args.args[1].model == "gpt-test"
        assert generate.call_args.args[1].reasoning_effort == "high"
        assert generate.call_args.args[0]._account_id == "private-account"
        assert run.json()["output"]["text"] == "Pilot note complete."
        assert run.json()["run"]["tokens_used"] == 13
        detail = client.get(f"{base}/runs/{run.json()['run']['id']}")
        assert detail.status_code == 200
        assert detail.json()["profile_snapshots"][0]["profile_reasoning_effort"] == "high"
        assert any(
            event["event_type"] == "live_text_started" and event["payload"]["reasoning_effort"] == "high"
            for event in detail.json()["events"]
        )
        tool_update = client.patch(
            f"{base}/profiles/{profile.json()['id']}",
            json={"allowed_tools": ["create_draft_work_order_v1"]},
            headers=ORIGIN,
        )
        assert tool_update.status_code == 200, tool_update.text
        tool_order = client.post(
            f"{base}/work-orders",
            json={
                "title": "Create follow-up work orders",
                "instructions": "Use the configured work-order tool to create a draft",
                "profile_id": profile.json()["id"],
            },
            headers=ORIGIN,
        )
        assert tool_order.status_code == 201, tool_order.text
        tool_order_id = tool_order.json()["id"]
        assert client.post(f"{base}/work-orders/{tool_order_id}/ready", headers=ORIGIN).status_code == 200
        with patch("onefoundry.inference.subscription_codex.CodexAccountTransport.generate") as generate:
            blocked = client.post(f"{base}/work-orders/{tool_order_id}/live-text-run", headers=ORIGIN)
        assert blocked.status_code == 409, blocked.text
        assert "tools" in blocked.json()["detail"].lower()
        generate.assert_not_called()
        saved_order = client.get(f"{base}/work-orders").json()
        assert next(item for item in saved_order if item["id"] == tool_order_id)["status"] == "blocked"
        assert not any(item["work_order_id"] == tool_order_id for item in client.get(f"{base}/runs").json())
        with patch(
            "onefoundry.model_gateway.discover_codex_models",
            return_value=[DiscoveredModel("gpt-test", "GPT Test", "text", ("low",))],
        ):
            assert client.post(f"{model}/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 200
        stale_order = client.post(
            f"{base}/work-orders",
            json={
                "title": "Stale effort",
                "instructions": "Write a note",
                "profile_id": profile.json()["id"],
            },
            headers=ORIGIN,
        )
        assert stale_order.status_code == 201
        assert (
            client.post(f"{base}/work-orders/{stale_order.json()['id']}/ready", headers=ORIGIN).status_code
            == 200
        )
        with patch("onefoundry.inference.subscription_codex.CodexAccountTransport.generate") as no_generate:
            stale_run = client.post(
                f"{base}/work-orders/{stale_order.json()['id']}/live-text-run", headers=ORIGIN
            )
        assert stale_run.status_code == 409
        no_generate.assert_not_called()
        assert "private-access" not in run.text


def test_fresh_codex_discovery_version_errors_and_saved_catalog(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    challenge = DeviceChallenge(
        "https://auth.openai.com/codex/device", "ABCD-EFGH", int(time.time()) + 600, 5, "private-device"
    )
    connected = {
        "kind": "connected",
        "access_token": "private-access",
        "refresh_token": "private-refresh",
        "account_id": "private-account",
        "expires_at": int(time.time()) + 3600,
    }
    requests: list[Request] = []
    mode = "success"

    def http(outbound: Request, *, timeout: float) -> HTTPResult:
        requests.append(outbound)
        assert outbound.method == "GET" and outbound.data is None
        assert outbound.full_url.endswith("client_version=0.160.0")
        if mode == "transport":
            raise TransportError("private-detail")
        if mode in {"401", "503"}:
            return HTTPResult(int(mode), b"private-upstream-body")
        if mode == "malformed":
            return HTTPResult(200, b"private-invalid-json")
        models = (
            []
            if mode == "empty"
            else [
                {
                    "slug": "gpt-6.1-sol",
                    "display_name": "GPT-6.1 Sol",
                    "supported_reasoning_levels": [{"effort": "ultra"}],
                },
                {"slug": "hidden-model", "visibility": "hide"},
            ]
        )
        return HTTPResult(200, json.dumps({"models": models}).encode())

    def discover(credential: dict[str, object], config: Settings) -> object:
        return discover_codex_models(credential, config, http)

    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Catalog version"}, headers=ORIGIN).json()["id"]
        other = client.post("/api/projects", json={"name": "Other catalog"}, headers=ORIGIN).json()["id"]
        base = f"/api/projects/{project}/model"
        with patch("onefoundry.model_gateway.start_device_login", return_value=challenge):
            account_id = client.post(f"{base}/codex/start", json={"label": "Codex"}, headers=ORIGIN).json()[
                "account"
            ]["id"]
        with patch("onefoundry.model_gateway.finish_device_login", return_value=connected):
            assert (
                client.post(f"{base}/accounts/{account_id}/codex/finish", headers=ORIGIN).status_code == 200
            )
        with patch("onefoundry.model_gateway.discover_codex_models", side_effect=discover):
            path = f"{base}/accounts/{account_id}/refresh"
            assert client.post(path).status_code == 403
            assert (
                client.post(
                    f"/api/projects/{other}/model/accounts/{account_id}/refresh", headers=ORIGIN
                ).status_code
                == 404
            )
            assert not requests
            fresh = client.post(path, headers=ORIGIN)
            assert fresh.status_code == 200, fresh.text
            assert fresh.json()["account"]["model_count"] == 1
            assert fresh.json()["models"][0]["model_id"] == "gpt-6.1-sol"
            assert fresh.json()["models"][0]["reasoning_efforts"] == ["ultra"]
            assert fresh.json()["models"][0]["available"] is True
            last_refresh = fresh.json()["account"]["last_refreshed_at"]
            for mode in ["401", "503", "transport", "malformed"]:
                failure = client.post(path, headers=ORIGIN)
                assert failure.status_code == 502, failure.text
                assert "private" not in failure.text
                if mode in {"401", "503"}:
                    assert f"HTTP {mode}" in failure.text
                if mode == "transport":
                    assert "transport failed" in failure.text and "reconnection" not in failure.text
                saved = client.get(f"{base}/catalog").json()
                assert saved[0]["model_id"] == "gpt-6.1-sol" and saved[0]["available"] is False
                account = client.get(f"{base}/accounts").json()[0]
                assert account["catalog_valid"] is False and account["last_refreshed_at"] == last_refresh
                assert decode_state((root / account_id).read_text()) == connected
            mode = "success"
            assert client.post(path, headers=ORIGIN).json()["models"][0]["available"] is True
            mode = "empty"
            empty = client.post(path, headers=ORIGIN)
            assert empty.status_code == 200 and empty.json()["models"] == []
            assert empty.json()["account"]["model_count"] == 0
            assert client.get(f"{base}/catalog").json() == []
            assert len(requests) == 7
