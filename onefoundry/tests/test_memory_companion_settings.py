"""Deployment inputs fail closed and never load private credentials automatically."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from onefoundry.memory_companion_settings import MemoryCompanionSettings


def values(tmp_path: Path) -> dict[str, Any]:
    private = tmp_path / "private.env"
    private.write_text("# Synthetic fixture, no credentials\n")
    private.chmod(0o600)
    public = tmp_path / "public.pem"
    public.write_text("Public key is validated by the service, not the deployment preflight.\n")
    public.chmod(0o644)
    return {
        "runtime_image": "localhost/memory-fixture@sha256:" + "a" * 64,
        "private_env_file": private,
        "public_key_host_path": public,
        "public_key_container_path": Path("/memory-public.pem"),
        "database_network": "isolated_memory_fixture",
    }


def test_valid_inputs_and_explicit_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = values(tmp_path)
    for name, value in expected.items():
        monkeypatch.setenv(f"MEMORY_{name.upper()}", str(value))
    configured = MemoryCompanionSettings()  # type: ignore[call-arg]
    assert configured.database_network == expected["database_network"]
    configured.validate_host_files()
    override = MemoryCompanionSettings(database_network="explicit_fixture")  # type: ignore[call-arg]
    assert override.database_network == "explicit_fixture"


def test_missing_inputs_and_dotenv_are_not_silently_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in MemoryCompanionSettings.model_fields:
        monkeypatch.delenv(f"MEMORY_{name.upper()}", raising=False)
    (tmp_path / ".env").write_text("MEMORY_DATABASE_NETWORK=unapproved_fixture\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        MemoryCompanionSettings()  # type: ignore[call-arg]


def test_prebuilt_local_release_image_can_be_pinned_by_id(tmp_path: Path) -> None:
    configured = MemoryCompanionSettings.model_validate(
        {**values(tmp_path), "runtime_image": "sha256:" + "b" * 64}
    )
    configured.validate_host_files()


@pytest.mark.parametrize(
    ("name", "invalid"),
    [
        ("runtime_image", "localhost/memory:latest"),
        ("runtime_image", "localhost/memory@sha256:" + "x" * 64),
        ("private_env_file", "relative.env"),
        ("public_key_host_path", "/"),
        ("public_key_container_path", "/memory/../key.pem"),
        ("public_key_container_path", "/${UNAPPROVED}/key.pem"),
        ("database_network", "fixture\nother"),
        ("database_network", ""),
    ],
)
def test_invalid_inputs_are_rejected(tmp_path: Path, name: str, invalid: str) -> None:
    with pytest.raises(ValidationError):
        MemoryCompanionSettings.model_validate({**values(tmp_path), name: invalid})


@pytest.mark.parametrize("kind", ["missing", "public_env", "env_symlink", "key_symlink", "writable_key"])
def test_unsafe_host_files_are_rejected(tmp_path: Path, kind: str) -> None:
    configured = MemoryCompanionSettings.model_validate(values(tmp_path))
    if kind == "missing":
        configured.private_env_file.unlink()
    elif kind == "public_env":
        configured.private_env_file.chmod(0o644)
    elif kind in {"env_symlink", "key_symlink"}:
        target = configured.private_env_file if kind == "env_symlink" else configured.public_key_host_path
        actual = target.with_suffix(".actual")
        target.rename(actual)
        target.symlink_to(actual)
    else:
        configured.public_key_host_path.chmod(0o666)
    with pytest.raises((ValueError, OSError)):
        configured.validate_host_files()
