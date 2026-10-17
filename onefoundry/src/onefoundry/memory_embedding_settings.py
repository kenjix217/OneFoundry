"""Typed local native embedding configuration owned by Settings."""

from typing import Literal, Protocol


class EmbeddingConfiguration(Protocol):
    memory_search_mode: Literal["lexical", "hybrid"]
    memory_embedding_runtime_path: str
    memory_embedding_runtime_sha256: str
    memory_embedding_model_path: str
    memory_embedding_model_sha256: str
    memory_embedding_model: str
    memory_embedding_context_length: int
    memory_embedding_dimensions: int
    memory_embedding_document_prefix: str
    memory_embedding_query_prefix: str
    memory_embedding_timeout_seconds: int
    memory_embedding_max_response_bytes: int
    memory_embedding_min_similarity: float
