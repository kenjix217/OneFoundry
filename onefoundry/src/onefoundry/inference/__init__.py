"""Opt-in text inference transports. Importing this package makes no network call."""

from .chat_compatible import CompatibleChatTransport
from .contracts import (
    AuthenticationError,
    IncompleteResponseError,
    InferenceError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TextResult,
    TransportError,
)
from .ollama import OllamaTextTransport
from .responses import OpenAIResponsesTransport, XAIResponsesTransport

__all__ = [
    "AuthenticationError",
    "CompatibleChatTransport",
    "IncompleteResponseError",
    "InferenceError",
    "InvalidRequestError",
    "OllamaTextTransport",
    "OpenAIResponsesTransport",
    "ProviderError",
    "RateLimitError",
    "TextRequest",
    "TextResult",
    "TransportError",
    "XAIResponsesTransport",
]
