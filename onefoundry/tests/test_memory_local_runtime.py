"""The local bootstrap resolves only the explicitly installed private database."""

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from onefoundry.memory_local_runtime import MemoryLocalRuntimeSettings, database_host


def test_settings_required_invalid_and_environment_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        MemoryLocalRuntimeSettings()  # type: ignore[call-arg]
    monkeypatch.setenv("MEMORY_LOCAL_DATABASE_CONTAINER", "fixture-db")
    monkeypatch.setenv("MEMORY_LOCAL_DATABASE_NETWORK", "fixture_network")
    configured = MemoryLocalRuntimeSettings()  # type: ignore[call-arg]
    assert configured.database_container == "fixture-db"
    assert MemoryLocalRuntimeSettings(database_container="explicit-db").database_container == "explicit-db"  # type: ignore[call-arg]
    for invalid in ("", "untrusted;command", "--flag", "new\nline"):
        with pytest.raises(ValidationError):
            MemoryLocalRuntimeSettings(database_container=invalid)  # type: ignore[call-arg]


@pytest.mark.parametrize("address", ["172.20.0.2", "127.0.0.1", "0.0.0.0", "8.8.8.8", "invalid"])  # noqa: S104
def test_private_endpoint_and_bounded_inspection(address: str) -> None:
    configured = MemoryLocalRuntimeSettings(database_container="fixture-db", database_network="fixture_net")
    result: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
        [], 0, json.dumps({"fixture_net": {"IPAddress": address}}).encode()
    )
    with patch("onefoundry.memory_local_runtime.subprocess.run", return_value=result) as run:
        if address == "172.20.0.2":
            assert database_host(configured, deadline_seconds=5) == address
            assert run.call_args.kwargs["timeout"] == 5
            assert run.call_args.args[0][-1] == "fixture-db"
        else:
            with pytest.raises(ValueError):
                database_host(configured, deadline_seconds=5)


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("docker", 5), subprocess.CalledProcessError(1, "docker"), OSError("missing")],
)
def test_dependency_failure_is_not_hidden(failure: Any) -> None:
    configured = MemoryLocalRuntimeSettings(database_container="fixture-db", database_network="fixture_net")
    with patch("onefoundry.memory_local_runtime.subprocess.run", side_effect=failure):
        with pytest.raises(type(failure)):
            database_host(configured, deadline_seconds=5)
