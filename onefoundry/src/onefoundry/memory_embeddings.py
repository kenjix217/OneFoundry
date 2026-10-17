"""Task prefixes and validated normalized local native embeddings."""

import hashlib
import json
import math
from typing import Literal

from onefoundry.gateway_errors import Unavailable
from onefoundry.memory_embedding_settings import EmbeddingConfiguration
from onefoundry.memory_native_runtime import NativeSession


def model_key(settings: EmbeddingConfiguration) -> str:
    """Separate vectors when weights, runtime, context or task-prefix policy changes."""
    return hashlib.sha256(
        json.dumps(
            [
                settings.memory_embedding_model,
                settings.memory_embedding_model_sha256,
                settings.memory_embedding_runtime_sha256,
                settings.memory_embedding_context_length,
                settings.memory_embedding_dimensions,
                settings.memory_embedding_document_prefix,
                settings.memory_embedding_query_prefix,
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LocalEmbeddings:
    def __init__(self, settings: EmbeddingConfiguration) -> None:
        self.settings = settings

    def embed(self, texts: list[str], purpose: Literal["document", "query"]) -> list[list[float]]:
        if not 1 <= len(texts) <= 16 or any(
            not value.strip() or len(value.encode()) > 4096 for value in texts
        ):
            raise ValueError("embedding inputs must be 1–16 bounded nonempty texts")
        prefix = (
            self.settings.memory_embedding_document_prefix
            if purpose == "document"
            else self.settings.memory_embedding_query_prefix
        )
        with NativeSession(self.settings) as runtime:
            response = runtime.exchange(
                "embed",
                {
                    "texts": [prefix + value for value in texts],
                    "truncate": False,
                },
            )
        if (
            response.get("model") != self.settings.memory_embedding_model
            or response.get("dimensions") != self.settings.memory_embedding_dimensions
        ):
            raise Unavailable("memory_embedding_model_mismatch")
        payload = response.get("embeddings")
        if not isinstance(payload, list) or len(payload) != len(texts):
            raise Unavailable("memory_embedding_response_invalid")
        vectors: list[list[float]] = []
        for raw in payload:
            if not isinstance(raw, list) or len(raw) != self.settings.memory_embedding_dimensions:
                raise Unavailable("memory_embedding_dimensions_mismatch")
            if any(type(value) not in (int, float) for value in raw):
                raise Unavailable("memory_embedding_response_invalid")
            try:
                values = [float(value) for value in raw]
            except (ValueError, OverflowError):
                raise Unavailable("memory_embedding_response_invalid") from None
            if any(not math.isfinite(value) for value in values):
                raise Unavailable("memory_embedding_response_invalid")
            norm = math.hypot(*values)
            if not math.isfinite(norm) or norm <= 0:
                raise Unavailable("memory_embedding_response_invalid")
            vectors.append([value / norm for value in values])
        return vectors
