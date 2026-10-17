"""Subscription credentials stay in the supervisor and never enter coding context."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from runner.repository_provider_policy import ProviderBounds, admit_request
from runner.repository_runner_credentials import broker_credential
from runner.repository_runner_settings import RepositoryRunnerSettings
from tests.test_repository_code import job_at
from tests.test_repository_provider import config_at


def test_coding_worker_ignores_other_workers_larger_job_contracts(tmp_path: Path) -> None:
    from runner.repository_code_supervisor import pending

    config = configured(tmp_path)
    for path in (config.spool / "jobs").glob("*.json"):
        path.unlink()
    (config.spool / "jobs" / f"{uuid4()}.json").write_text(
        json.dumps({"kind": "extension_release_prepare_v1", "fixture": "x" * 9000})
    )
    bridge = MagicMock()
    assert pending(config, bridge) == 0
    bridge.validate_job.assert_not_called()


def configured(tmp_path: Path) -> RepositoryRunnerSettings:
    job, root, spool = job_at(tmp_path)
    config = config_at(tmp_path, job, root, spool, job.target.test_image, job.target.test_image)
    config.credential_file.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "fixture-access",
                    "account_id": "fixture-account",
                    "refresh_token": "must-stay-outside-broker",
                },
            }
        )
    )
    return RepositoryRunnerSettings.model_validate(
        {
            **config.model_dump(),
            "credential_file": config.credential_file,
            "auth_kind": "codex_subscription",
            "provider_url": "https://chatgpt.com/backend-api/codex/responses",
        }
    )


def test_subscription_extracts_only_access_material_without_mutating_auth(tmp_path: Path) -> None:
    config = configured(tmp_path)
    original = config.credential_file.read_bytes()
    token, account = broker_credential(config)
    assert token == b"fixture-access" and account == "fixture-account"
    assert config.credential_file.read_bytes() == original
    assert b"must-stay-outside-broker" not in token
    assert "credential_file" not in config.model_dump()


@pytest.mark.parametrize(
    "route",
    ["https://example.com/responses", "https://api.openai.com/v1/responses", "https://chatgpt.com/other"],
)
def test_subscription_never_falls_back_to_another_credential_route(tmp_path: Path, route: str) -> None:
    config = configured(tmp_path)
    with pytest.raises(ValidationError):
        RepositoryRunnerSettings.model_validate(
            {
                **config.model_dump(),
                "credential_file": config.credential_file,
                "provider_url": route,
            }
        )


@pytest.mark.parametrize(
    "tokens", [{}, {"access_token": "fixture"}, {"access_token": "fixture\r\n", "account_id": "fixture"}]
)
def test_invalid_subscription_is_rejected_before_launch(tmp_path: Path, tokens: dict[str, str]) -> None:
    config = configured(tmp_path)
    config.credential_file.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))
    with pytest.raises(ValueError, match="credential"):
        broker_credential(config)


def test_subscription_request_uses_its_actual_protocol_and_keeps_local_tools() -> None:
    bounds = ProviderBounds("fixture-model", 2, 4096, 4096, 64, auth_kind="codex_subscription")
    value = {
        "model": "fixture-model",
        "stream": True,
        "tools": [{"type": "function", "name": "exec_command"}],
    }
    admitted = json.loads(admit_request(json.dumps(value).encode(), bounds, 0))
    assert "max_output_tokens" not in admitted and admitted["store"] is False
    with pytest.raises(ValueError, match="output_field"):
        admit_request(json.dumps({**value, "max_output_tokens": 100}).encode(), bounds, 0)
    with pytest.raises(ValueError, match="tool_denied"):
        admit_request(json.dumps({**value, "tools": [{"type": "web_search"}]}).encode(), bounds, 0)


@pytest.mark.parametrize(
    "auth,media,allowed",
    [
        ("codex_subscription", "", True),
        ("codex_subscription", "text/event-stream", True),
        ("api_key", "", False),
        ("codex_subscription", "application/json", False),
    ],
)
def test_subscription_may_omit_mime_but_still_requires_terminal_sse(
    auth: str, media: str, allowed: bool
) -> None:
    from runner.repository_provider_broker import forward

    config = {
        "upstream_url": "https://chatgpt.com/backend-api/codex/responses",
        "auth_kind": auth,
        "account_id": "fixture-account",
        "request_timeout_seconds": 1,
        "max_response_bytes": 4096,
    }
    response = MagicMock()
    response.status = 200
    response.getheader.return_value = media
    body = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
    response.read.return_value = body
    with (
        patch(
            "runner.repository_provider_broker.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("8.8.8.8", 443))],
        ),
        patch("runner.repository_provider_broker.socket.create_connection"),
        patch("runner.repository_provider_broker.ssl.create_default_context"),
        patch("runner.repository_provider_broker.http.client.HTTPResponse", return_value=response),
    ):
        if allowed:
            assert forward(config, b"fixture-credential", b"{}") == body
            response.read.return_value = b'data: {"type":"response.created"}\n\n'
            with pytest.raises(ValueError, match="terminal_missing"):
                forward(config, b"fixture-credential", b"{}")
        else:
            with pytest.raises(ValueError, match="response_invalid"):
                forward(config, b"fixture-credential", b"{}")
