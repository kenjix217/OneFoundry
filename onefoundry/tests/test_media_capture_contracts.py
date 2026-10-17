"""Local video bounds, configuration precedence and immutable byte ownership."""

import hashlib
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.artifact_workspace import (
    ArtifactAlreadyExists,
    ArtifactCorrupt,
    ArtifactInvalid,
    ArtifactWorkspace,
)
from onefoundry.media_capture_store import PlaybackVerification
from onefoundry.role_video_capture import CaptureRequest
from onefoundry.settings import Settings


# Container identification fixture only; real decoding is proved in Chromium.
class _VideoReadArguments(TypedDict):
    relative_path: str
    expected_sha256: str
    expected_byte_size: int
    kind: str
    mime_type: str
    max_video_bytes: int


WEBM_HEADER = b"\x1a\x45\xdf\xa3\x42\x82\x84webm\x00\x00"


def test_video_storage_requires_explicit_bound_and_verifies_exact_bytes(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    owner = ArtifactWorkspace(root)
    project, run, artifact = uuid4(), uuid4(), uuid4()
    with pytest.raises(ArtifactInvalid, match="explicit bounded"):
        owner.write(project, run, artifact, kind="video", mime_type="video/webm", content=WEBM_HEADER)
    saved = owner.write(
        project,
        run,
        artifact,
        kind="video",
        mime_type="video/webm",
        content=WEBM_HEADER,
        max_video_bytes=1024,
    )
    assert saved.relative_path.endswith(".webm")
    args: _VideoReadArguments = {
        "relative_path": saved.relative_path,
        "expected_sha256": saved.sha256,
        "expected_byte_size": saved.byte_size,
        "kind": "video",
        "mime_type": "video/webm",
        "max_video_bytes": 1024,
    }
    assert owner.read(project, run, artifact, **args) == WEBM_HEADER
    with pytest.raises(ArtifactAlreadyExists):
        owner.write(
            project,
            run,
            artifact,
            kind="video",
            mime_type="video/webm",
            content=WEBM_HEADER,
            max_video_bytes=1024,
        )
    (root / saved.relative_path).write_bytes(WEBM_HEADER[:-1] + b"x")
    with pytest.raises(ArtifactCorrupt, match="do not match"):
        owner.read(project, run, artifact, **args)
    assert saved.sha256 == hashlib.sha256(WEBM_HEADER).hexdigest()


@pytest.mark.parametrize("content", [b"", b"not video", b"\x1aE\xdf\xa3matroska", WEBM_HEADER * 100])
def test_video_rejects_empty_wrong_container_and_oversized_input(tmp_path: Path, content: bytes) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    with pytest.raises(ArtifactInvalid):
        ArtifactWorkspace(root).write(
            uuid4(),
            uuid4(),
            uuid4(),
            kind="video",
            mime_type="video/webm",
            content=content,
            max_video_bytes=1024,
        )


def test_media_settings_defaults_validation_and_environment_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "MEDIA_CAPTURE_ENABLED",
        "MEDIA_MAX_BYTES",
        "MEDIA_MAX_DURATION_SECONDS",
        "MEDIA_MAX_WIDTH",
        "MEDIA_MAX_HEIGHT",
        "MEDIA_FRAME_RATE",
    ):
        monkeypatch.delenv(name, raising=False)
    parameters: dict[str, Any] = {"_env_file": None, "db_password": "fixture-only"}
    defaults = Settings(**parameters)
    assert defaults.media_capture_enabled is True
    assert defaults.media_max_bytes == 33_554_432 and defaults.media_frame_rate == 24
    monkeypatch.setenv("MEDIA_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_MAX_BYTES", "2048")
    assert Settings(**parameters).media_capture_enabled is True
    assert Settings(**parameters).media_max_bytes == 2048
    assert Settings(**{**parameters, "media_max_bytes": 4096}).media_max_bytes == 4096
    for field, value in {
        "media_max_bytes": 67_108_865,
        "media_max_duration_seconds": 0,
        "media_max_width": 319,
        "media_max_height": 1081,
        "media_frame_rate": 31,
    }.items():
        with pytest.raises(ValidationError):
            Settings(**{**parameters, field: value})
    monkeypatch.setenv("MEDIA_CAPTURE_ENABLED", "invalid")
    with pytest.raises(ValidationError):
        Settings(**parameters)


def test_capture_and_playback_decisions_reject_unsafe_or_false_claims() -> None:
    with pytest.raises(ValidationError):
        CaptureRequest(title="Demo", caption="postgresql://alice:password@db/private")
    with pytest.raises(ValidationError):
        PlaybackVerification.model_validate(
            {
                "expected_sha256": "a" * 64,
                "watched_picture_and_captions": False,
                "narration_reviewed_if_present": True,
            }
        )
