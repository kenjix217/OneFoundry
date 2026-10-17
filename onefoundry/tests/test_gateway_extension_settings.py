"""Extension policy is typed, deny-by-default, and follows Settings precedence."""

import pytest
from pydantic import ValidationError

from onefoundry.settings import Settings


def test_missing_extension_configuration_denies_network_and_python() -> None:
    settings = Settings(db_password="test-only")
    assert settings.extension_allowed_origins == []
    assert settings.extension_worker_image == ""


def test_worker_image_environment_precedence_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    pinned = "sha256:" + "a" * 64
    monkeypatch.setenv("EXTENSION_WORKER_IMAGE", pinned)
    assert Settings(db_password="test-only").extension_worker_image == pinned
    assert Settings(db_password="test-only", extension_worker_image="").extension_worker_image == ""
    for invalid in ("python:latest", "sha256:short", "sha256:" + "A" * 64):
        monkeypatch.setenv("EXTENSION_WORKER_IMAGE", invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


@pytest.mark.parametrize(
    "origins",
    [
        ["http://localhost:8765/path"],
        ["https://x.example/?key=value"],
        ["https://x.example", "https://x.example"],
        ["ftp://x.example"],
    ],
)
def test_invalid_extension_origins_fail(origins: list[str]) -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", extension_allowed_origins=origins)


def test_extension_environment_and_explicit_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTENSION_ALLOWED_ORIGINS", '["https://service.example"]')
    monkeypatch.setenv("EXTENSION_TIMEOUT_SECONDS", "15")
    settings = Settings(db_password="test-only")
    assert settings.extension_allowed_origins == ["https://service.example"]
    assert settings.extension_timeout_seconds == 15
    assert Settings(db_password="test-only", extension_timeout_seconds=8).extension_timeout_seconds == 8
    monkeypatch.setenv("EXTENSION_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


@pytest.mark.parametrize(
    ("field", "default", "valid", "too_low", "too_high"),
    [
        ("extension_webhook_max_bytes", 65536, 4096, 1023, 1048577),
        ("extension_webhook_window_seconds", 300, 120, 29, 901),
        ("extension_worker_heartbeat_seconds", 30, 60, 4, 301),
        ("extension_worker_queue_seconds", 60, 90, 4, 601),
        ("extension_timeout_seconds", 10, 20, 0, 121),
        ("extension_max_input_bytes", 65536, 4096, 1023, 1048577),
        ("extension_max_response_bytes", 262144, 4096, 1023, 4194305),
        ("extension_worker_memory_mb", 256, 512, 127, 1025),
        ("extension_worker_processes", 32, 16, 7, 65),
        ("extension_worker_cpus", 1, 2, 0, 3),
        ("extension_worker_max_output_bytes", 65536, 4096, 1023, 65537),
    ],
)
def test_webhook_settings_default_environment_precedence_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    default: int,
    valid: int,
    too_low: int,
    too_high: int,
) -> None:
    name = field.upper()
    monkeypatch.delenv(name, raising=False)
    assert getattr(Settings(db_password="test-only"), field) == default
    monkeypatch.setenv(name, str(valid))
    assert getattr(Settings(db_password="test-only"), field) == valid
    assert getattr(Settings.model_validate({"db_password": "test-only", field: default}), field) == default
    for bad in (str(too_low), str(too_high), "invalid"):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_extension_worker_poll_default_environment_precedence_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "EXTENSION_WORKER_POLL_SECONDS"
    monkeypatch.delenv(name, raising=False)
    assert Settings(db_password="test-only").extension_worker_poll_seconds == 0.25
    monkeypatch.setenv(name, "0.5")
    assert Settings(db_password="test-only").extension_worker_poll_seconds == 0.5
    assert (
        Settings(db_password="test-only", extension_worker_poll_seconds=1).extension_worker_poll_seconds == 1
    )
    for bad in ("0.09", "10.1", "invalid"):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")
