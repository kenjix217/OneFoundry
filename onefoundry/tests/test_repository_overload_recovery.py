"""Only explicit overload rejection can retry an undelivered provider response."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from runner import repository_provider_broker as broker
from runner.repository_provider_diagnostics import summarize
from runner.repository_provider_policy import ProviderBounds
from runner.repository_runner_settings import RepositoryRunnerSettings
from tests.test_repository_code import job_at
from tests.test_repository_provider import config_at


def requests(*, budget: int = 3, retries: int = 1) -> broker.BrokerRequests:
    return broker.BrokerRequests(
        {"provider_overload_retries": retries, "provider_overload_retry_delay_seconds": 0.1},
        b"private-token",
        ProviderBounds("fixture", budget, 4096, 4096, 100),
    )


def payload() -> bytes:
    return json.dumps(
        {"model": "fixture", "stream": True, "tools": [{"type": "function", "name": "exec_command"}]}
    ).encode()


def test_exact_overload_retries_once_before_delivering_response(capsys: pytest.CaptureFixture[str]) -> None:
    owner = requests()
    with (
        patch.object(
            broker,
            "forward",
            side_effect=[ValueError("repository_provider_rejected_server_is_overloaded"), b"completed"],
        ) as send,
        patch("runner.repository_provider_broker.time.sleep") as pause,
    ):
        assert owner.send(payload()) == b"completed"
        assert owner.requests == 2 and send.call_count == 2
        pause.assert_called_once_with(0.1)
    logs = capsys.readouterr().out.encode()
    assert summarize(logs)["retried_requests"] == 1
    assert b"private" not in logs


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ValueError("repository_provider_terminal_missing"),
        ValueError("repository_provider_incomplete"),
        ValueError("repository_provider_rejected_unknown"),
        ValueError("repository_provider_rejected_rate_limit_exceeded"),
    ],
)
def test_uncertain_or_other_rejections_never_retry(error: Exception) -> None:
    owner = requests()
    with (
        patch.object(broker, "forward", side_effect=error) as send,
        patch("runner.repository_provider_broker.time.sleep") as pause,
    ):
        with pytest.raises(type(error)):
            owner.send(payload())
        assert send.call_count == owner.requests == 1
        pause.assert_not_called()


@pytest.mark.parametrize("budget,retries,expected", [(3, 1, 2), (3, 0, 1), (1, 1, 1)])
def test_retries_obey_both_existing_request_budget_and_configured_retry_bound(
    budget: int, retries: int, expected: int
) -> None:
    owner = requests(budget=budget, retries=retries)
    with (
        patch.object(
            broker, "forward", side_effect=ValueError("repository_provider_rejected_server_is_overloaded")
        ) as send,
        patch("runner.repository_provider_broker.time.sleep"),
    ):
        with pytest.raises(ValueError):
            owner.send(payload())
        assert send.call_count == owner.requests == expected


def test_runner_retry_defaults_explicit_override_and_invalid_values(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)
    config = config_at(tmp_path, job, root, spool, job.target.test_image, job.target.test_image)
    assert config.provider_overload_retries == 1
    assert config.provider_overload_retry_delay_seconds == 1
    values = {**config.model_dump(), "credential_file": str(config.credential_file)}
    assert (
        RepositoryRunnerSettings.model_validate(
            {**values, "provider_overload_retries": 0}
        ).provider_overload_retries
        == 0
    )
    for changed in (
        {"provider_overload_retries": -1},
        {"provider_overload_retries": 3},
        {"provider_overload_retry_delay_seconds": 0},
        {"provider_overload_retry_delay_seconds": 31},
    ):
        with pytest.raises(ValidationError):
            RepositoryRunnerSettings.model_validate({**values, **changed})
