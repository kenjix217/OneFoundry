"""Native model contract, configuration, subprocess bounds and failure identity."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from onefoundry.gateway_errors import Unavailable
from onefoundry.memory_embeddings import LocalEmbeddings, model_key
from onefoundry.memory_native_runtime import _INFERENCE_LOCK, NativeSession
from onefoundry.settings import Settings


def configured(**changes: Any) -> Settings:
    return Settings.model_validate(
        {
            "db_password": "synthetic-test-only",
            "memory_search_mode": "hybrid",
            "memory_embedding_runtime_path": "/synthetic/native-embedding",
            "memory_embedding_runtime_sha256": "a" * 64,
            "memory_embedding_model_path": "/synthetic/model.gguf",
            "memory_embedding_model_sha256": "b" * 64,
            "memory_embedding_model": "fixture@revision1",
            "memory_embedding_dimensions": 2,
            "memory_embedding_document_prefix": "search_document: ",
            "memory_embedding_query_prefix": "search_query: ",
            **changes,
        }
    )


def worker(tmp_path: Path, *, mode: str = "", payload: Any = None) -> Settings:
    runtime = tmp_path / "native-fixture"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"public synthetic model fixture")
    runtime.write_text(
        """#!/usr/bin/python3
import json,os,signal,sys
mode = MODE
vectors = PAYLOAD
for line in sys.stdin:
    request=json.loads(line)
    with open(LOG, 'a') as stream: stream.write(json.dumps(request)+'\\n')
    if mode == 'hang': signal.pause()
    if mode == 'exit': sys.exit(1)
    reply={'ok':True,'type':request['type'],'request_id':request['request_id'],
           'model':request.get('model','fixture@revision1'),'provider':'native',
           'dimensions':2,'context_length':2048}
    if mode == 'wrong_model': reply['model']='wrong'
    if mode == 'wrong_id': reply['request_id']='foreign'
    if mode == 'failure': reply.update(ok=False,error='protected-provider-text')
    if mode == 'oversized': reply['extra']='x'*5000
    if request['type']=='embed': reply['embeddings']=vectors
    print(json.dumps(reply),flush=True)
""".replace("MODE", repr(mode))
        .replace("PAYLOAD", f"json.loads({json.dumps(payload)!r})")
        .replace("LOG", repr(str(tmp_path / "requests.jsonl")))
    )
    runtime.chmod(0o700)
    model.chmod(0o600)
    return configured(
        memory_embedding_runtime_path=str(runtime),
        memory_embedding_runtime_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
        memory_embedding_model_path=str(model),
        memory_embedding_model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
    )


def test_settings_missing_invalid_and_explicit_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(db_password="fixture").memory_search_mode == "lexical"
    with pytest.raises(ValidationError):
        Settings(db_password="fixture", memory_search_mode="hybrid")
    monkeypatch.setenv("MEMORY_SEARCH_MODE", "hybrid")
    assert Settings(db_password="fixture", memory_search_mode="lexical").memory_search_mode == "lexical"
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL_PATH", "/approved/env-model.gguf")
    assert Settings(db_password="fixture", memory_search_mode="lexical").memory_embedding_model_path.endswith(
        "env-model.gguf"
    )
    assert (
        configured(memory_embedding_model_path="/explicit/model.gguf").memory_embedding_model_path
        == "/explicit/model.gguf"
    )
    for field, value in [
        ("memory_embedding_runtime_path", "relative/binary"),
        ("memory_embedding_model_path", "relative/model"),
        ("memory_embedding_runtime_sha256", "mutable"),
        ("memory_embedding_model_sha256", "x" * 64),
        ("memory_embedding_dimensions", 4001),
        ("memory_embedding_context_length", 8192),
        ("memory_embedding_timeout_seconds", 0),
        ("memory_embedding_max_response_bytes", 5),
        ("memory_embedding_min_similarity", 2),
    ]:
        with pytest.raises(ValidationError):
            configured(**{field: value})


def test_prefix_normalization_no_truncation_and_cleanup(tmp_path: Path) -> None:
    settings = worker(tmp_path, payload=[[3, 4]])
    client = LocalEmbeddings(settings)
    assert client.embed(["Company fact"], "document") == [[0.6, 0.8]]
    assert client.embed(["Meaning question"], "query") == [[0.6, 0.8]]
    calls = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert calls[1]["texts"] == ["search_document: Company fact"] and calls[1]["truncate"] is False
    assert calls[3]["texts"] == ["search_query: Meaning question"]
    for inputs in [[], [""], ["x" * 4097], ["a"] * 17]:
        with pytest.raises(ValueError):
            client.embed(inputs, "document")
    assert model_key(settings) != model_key(configured(memory_embedding_model_sha256="c" * 64))
    assert model_key(configured()) != model_key(configured(memory_embedding_query_prefix="different: "))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        [[1]],
        [[True, 1]],
        [[0, 0]],
        [[float("nan"), 1]],
        [[float("inf"), 1]],
        [[10**400, 1]],
        [["protected-test-text", 1]],
        [[1, 0], [1, 0]],
    ],
)
def test_malformed_vectors_never_return_content(tmp_path: Path, payload: Any) -> None:
    with pytest.raises(Unavailable, match="memory_embedding_") as failure:
        LocalEmbeddings(worker(tmp_path, payload=payload)).embed(["fixture"], "query")
    assert "protected-test-text" not in str(failure.value)


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("wrong_model", "model_mismatch"),
        ("wrong_id", "response_invalid"),
        ("failure", "response_invalid"),
        ("exit", "unavailable"),
        ("oversized", "response_invalid"),
    ],
)
def test_dependency_and_protocol_failure(tmp_path: Path, mode: str, reason: str) -> None:
    settings = worker(tmp_path, mode=mode, payload=[[1, 0]])
    settings.memory_embedding_max_response_bytes = 4096
    with pytest.raises(Unavailable, match=reason) as failure:
        LocalEmbeddings(settings).embed(["fixture"], "query")
    assert "protected" not in str(failure.value)


def test_timeout_reaps_worker_and_releases_serialization(tmp_path: Path) -> None:
    settings = worker(tmp_path, mode="hang")
    settings.memory_embedding_timeout_seconds = 1
    session = NativeSession(settings)
    with pytest.raises(Unavailable, match="timeout"):
        with session:
            pass
    assert session.process is None and not session.locked
    assert LocalEmbeddings(worker(tmp_path, payload=[[1, 0]])).embed(["fixture"], "query") == [[1, 0]]


def test_invalid_asset_permissions_changed_hash_and_symlink(tmp_path: Path) -> None:
    settings = worker(tmp_path, payload=[[1, 0]])
    runtime = Path(settings.memory_embedding_runtime_path)
    runtime.chmod(0o777)
    with pytest.raises(Unavailable, match="asset_invalid"):
        LocalEmbeddings(settings).embed(["fixture"], "query")
    runtime.chmod(0o700)
    settings.memory_embedding_model_sha256 = "c" * 64
    with pytest.raises(Unavailable, match="asset_invalid"):
        LocalEmbeddings(settings).embed(["fixture"], "query")
    linked = tmp_path / "linked"
    linked.symlink_to(runtime)
    settings.memory_embedding_runtime_path = str(linked)
    with pytest.raises(Unavailable, match="asset_invalid"):
        LocalEmbeddings(settings).embed(["fixture"], "query")


def test_busy_worker_queue_is_bounded_and_cancellation_reaps_child(tmp_path: Path) -> None:
    settings = worker(tmp_path, payload=[[1, 0]])
    settings.memory_embedding_timeout_seconds = 1
    with _INFERENCE_LOCK:
        with pytest.raises(Unavailable, match="busy"):
            LocalEmbeddings(settings).embed(["fixture"], "query")
    session = NativeSession(settings)
    with pytest.raises(KeyboardInterrupt):
        with session:
            raise KeyboardInterrupt
    assert session.process is None and not session.locked
