"""Small, provider-neutral text inference contract."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field


class InferenceError(Exception):
    """A safe-to-display inference failure without provider body or credentials."""


class InvalidRequestError(InferenceError):
    pass


class AuthenticationError(InferenceError):
    pass


class RateLimitError(InferenceError):
    pass


class ProviderError(InferenceError):
    pass


class IncompleteResponseError(ProviderError):
    pass


class TransportError(InferenceError):
    pass


@dataclass(frozen=True, slots=True)
class InputImage:
    mime_type: str
    content: bytes

    def __post_init__(self) -> None:
        if self.mime_type not in {"image/png", "image/jpeg", "image/webp"} or not self.content:
            raise InvalidRequestError("input image is invalid")

    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{base64.b64encode(self.content).decode('ascii')}"


@dataclass(frozen=True, slots=True)
class TextRequest:
    model: str
    instructions: str
    prompt: str
    max_output_tokens: int
    timeout_seconds: float
    request_id: str
    reasoning_effort: str | None = None
    images: tuple[InputImage, ...] = ()
    progress_callback: Callable[[str], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.model.strip() or len(self.model) > 200 or not self.prompt.strip():
            raise InvalidRequestError("model and prompt are required")
        if not 1 <= self.max_output_tokens <= 131_072:
            raise InvalidRequestError("max_output_tokens is outside the supported range")
        if not 0 < self.timeout_seconds <= 600:
            raise InvalidRequestError("timeout_seconds is outside the supported range")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.request_id):
            raise InvalidRequestError("request_id is invalid")
        if self.reasoning_effort is not None and not re.fullmatch(
            r"[a-z][a-z0-9_]{0,31}", self.reasoning_effort
        ):
            raise InvalidRequestError("reasoning effort is invalid")


@dataclass(frozen=True, slots=True)
class TextResult:
    text: str
    provider_response_id: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    cost_usd_ticks: int | None = None
