"""Editing stays disabled until typed, pinned runtime configuration is supplied."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from onefoundry.settings import Settings


def test_disabled_default_and_missing_image() -> None:
    assert not Settings(db_password="test-only").video_edit_enabled
    with pytest.raises(ValidationError, match="pinned"):
        Settings(db_password="test-only", video_edit_enabled=True)


@pytest.mark.parametrize(
    "values",
    [
        {"video_edit_asset_root": "relative"},
        {"video_edit_asset_root": "/"},
        {"video_edit_image": "mutable:latest"},
        {"video_edit_docker_binary": "docker"},
        {"video_edit_docker_socket": "tcp://remote"},
        {"video_edit_docker_socket": "/var/../secret"},
        {"video_edit_docker_binary": "/usr/bin/docker\x00"},
        {"video_edit_wall_seconds": 0},
        {"video_edit_memory_mb": 8},
        {"video_edit_max_scenes": 33},
        {"video_edit_max_captions": -1},
        {"video_edit_max_jobs_per_order": 0},
        {"video_edit_max_attempts": 4},
        {"video_edit_cpus": 0},
        {"video_edit_pids": 1},
    ],
)
def test_invalid_configuration(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", **values)  # type: ignore[arg-type]


def test_environment_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIDEO_EDIT_ENABLED", "true")
    monkeypatch.setenv("VIDEO_EDIT_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setenv("VIDEO_EDIT_ASSET_ROOT", str(tmp_path))
    monkeypatch.setenv("VIDEO_EDIT_DOCKER_BINARY", str(tmp_path / "docker"))
    monkeypatch.setenv("VIDEO_EDIT_DOCKER_SOCKET", str(tmp_path / "docker.sock"))
    settings = Settings(db_password="test-only")
    assert settings.video_edit_enabled and settings.video_edit_asset_root == str(tmp_path)
    assert settings.video_edit_docker_binary == str(tmp_path / "docker")
    assert settings.video_edit_docker_socket == str(tmp_path / "docker.sock")
    explicit = Settings(db_password="test-only", video_edit_docker_socket="/explicit.sock")
    assert explicit.video_edit_docker_socket == "/explicit.sock"
    assert not Settings(db_password="test-only", video_edit_enabled=False).video_edit_enabled


def test_installed_compose_forwards_video_policy_and_owns_asset_storage() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text()
    app = compose.split("\n  app:\n", 1)[1].split("\n  onefoundry-coordinator:\n", 1)[0]
    example = (root / ".env.example").read_text()
    for field in Settings.model_fields:
        if field.startswith("video_edit_"):
            name = field.upper()
            assert f"      {name}: ${{{name}-" in app, f"installed app omits {name}"
            assert f"{name}=" in example
    assert "video_edit_assets:${VIDEO_EDIT_ASSET_ROOT-/video-edit-assets}" in app
    assert "video-edit-asset-init:" in app
