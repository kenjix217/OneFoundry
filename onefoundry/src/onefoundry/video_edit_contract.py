"""Provider-neutral, data-only recipes for bounded local video edits."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Scene(StrictModel):
    asset_id: UUID
    kind: Literal["image", "video"]
    start: float = Field(default=0, ge=0, le=300, allow_inf_nan=False)
    seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    fit: Literal["contain", "cover"] = "contain"
    position_x: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    position_y: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def image_start(self) -> Scene:
        if self.kind == "image" and self.start != 0:
            raise ValueError("image scenes cannot have a source trim")
        return self


class Caption(StrictModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=160)

    @field_validator("text")
    @classmethod
    def plain_text(cls, value: str) -> str:
        if any(ord(c) < 32 or c in "{}\\" for c in value) or contains_sensitive_text(value):
            raise ValueError("captions must be plain safe text")
        return value


class AudioTrack(StrictModel):
    asset_id: UUID
    kind: Literal["narration", "music"]
    start: float = Field(default=0, ge=0, le=300, allow_inf_nan=False)
    source_start: float = Field(default=0, ge=0, le=300, allow_inf_nan=False)
    seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    gain: float = Field(default=1, gt=0, le=1, allow_inf_nan=False)


class EditRequest(StrictModel):
    request_key: UUID
    width: int = Field(ge=320, le=1920)
    height: int = Field(ge=240, le=1920)
    scenes: list[Scene] = Field(min_length=1, max_length=32)
    captions: list[Caption] = Field(default_factory=list, max_length=128)
    audio: list[AudioTrack] = Field(default_factory=list, max_length=2)

    @property
    def duration(self) -> float:
        return sum(scene.seconds for scene in self.scenes)

    @model_validator(mode="after")
    def timeline(self) -> EditRequest:
        if self.width % 2 or self.height % 2:
            raise ValueError("export dimensions must be even")
        if self.duration > 300:
            raise ValueError("edit duration exceeds the contract ceiling")
        previous_end = 0.0
        for caption in self.captions:
            if caption.start < previous_end or not caption.start < caption.end <= self.duration:
                raise ValueError("captions must be ordered, nonoverlapping and inside the cut")
            previous_end = caption.end
        if len({track.kind for track in self.audio}) != len(self.audio):
            raise ValueError("at most one narration and one music track are supported")
        if any(track.start + track.seconds > self.duration for track in self.audio):
            raise ValueError("audio must finish inside the cut; extend the last scene")
        return self
