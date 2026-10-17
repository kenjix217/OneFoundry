"""Real PostgreSQL semantic recall, lifecycle, scope and index integrity."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from onefoundry.company_memory_schemas import MemoryTransition
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.main import create_app
from onefoundry.memory_embedding_index import index_project
from onefoundry.memory_plugin_gateway import MemoryFacadeError, MemoryPluginGateway
from onefoundry.memory_plugin_settings import MemoryGrant
from onefoundry.memory_schemas import CandidateReview
from onefoundry.tool_gateway import ToolGateway
from tests.memory_plugin_fixtures import policy
from tests.test_company_memory_postgres import capture, owner, publish
from tests.test_memory_embeddings import configured
from tests.test_task_session_postgres import _settings


def project(client: TestClient) -> UUID:
    return UUID(
        client.post(
            "/api/projects",
            json={"name": "Synthetic vector fixture"},
            headers={"origin": "http://localhost:8765"},
        ).json()["id"]
    )


def vectors(texts: list[str], purpose: str) -> list[list[float]]:
    return [
        [1.0, 0.0] if value in {"Copper customers renew annually", "subscription extension"} else [0.0, 1.0]
        for value in texts
    ]


def test_semantic_recall_scope_correction_withdrawal_and_index_failure(tmp_path: Path) -> None:
    gateway = owner()
    settings = configured()
    search = ToolGateway(gateway.engine, "operator", live_text_settings=settings)
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own, foreign = project(client), project(client)
        fact = publish(gateway, own, capture("Copper customers renew annually"))
        unrelated = publish(gateway, own, capture("Harbor ships arrive Friday"))
        hidden = publish(gateway, foreign, capture("Copper customers renew annually"))
        assert gateway.retrieve_memory(own, "subscription extension", 5, "project") == []
        with pytest.raises(Unavailable, match="index_incomplete"):
            search.retrieve_memory(own, "subscription extension", 5, "project")
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            assert index_project(gateway, settings, own, limit=16)["indexed"] == 2
            assert index_project(gateway, settings, foreign, limit=16)["indexed"] == 1
            assert index_project(gateway, settings, own, limit=16)["indexed"] == 0
            rows = search.retrieve_memory(own, "subscription extension", 5, "project")
            assert [item["id"] for item in rows] == [fact["id"]]
            assert rows[0]["similarity"] == 1 and rows[0]["score"] == 0
            assert hidden["id"] not in [item["id"] for item in rows]
            assert unrelated["id"] not in [item["id"] for item in rows]
            gateway.transition_memory(own, fact["id"], MemoryTransition(action="withdraw", reason="fixture"))
            assert search.retrieve_memory(own, "subscription extension", 5, "project") == []
            gateway.transition_memory(own, fact["id"], MemoryTransition(action="restore", reason="fixture"))
            assert search.retrieve_memory(own, "subscription extension", 5, "project")[0]["id"] == fact["id"]
            replacement = publish(
                gateway, own, capture("Copper customers renew annually", replaces_memory_id=fact["id"])
            )
            with pytest.raises(Unavailable, match="index_incomplete"):
                search.retrieve_memory(own, "subscription extension", 5, "project")
            assert index_project(gateway, settings, own, limit=16)["indexed"] == 1
            assert [
                item["id"] for item in search.retrieve_memory(own, "subscription extension", 5, "project")
            ] == [replacement["id"]]
            changed = configured(memory_embedding_model="fixture@different-weights")
            with pytest.raises(Unavailable, match="index_incomplete"):
                ToolGateway(gateway.engine, "operator", live_text_settings=changed).retrieve_memory(
                    own, "subscription extension", 5, "project"
                )
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=Unavailable("offline")):
            with pytest.raises(Unavailable, match="offline"):
                search.retrieve_memory(own, "subscription extension", 5, "project")
        grant = MemoryGrant(
            subject="synthetic", client_id="test", actor_id="external-memory:codex", project_ids=(own,)
        )
        facade = MemoryPluginGateway(
            policy(grants=(grant,)),
            {grant.actor_id: ToolGateway(gateway.engine, grant.actor_id, live_text_settings=settings)},
        )
        with pytest.raises(MemoryFacadeError, match="authorization_denied"):
            facade.invoke(
                grant, "memory_search", {"project_id": str(foreign), "query": "subscription extension"}
            )
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            result = facade.invoke(
                grant, "memory_search", {"project_id": str(own), "query": "subscription extension"}
            )
            assert result["items"][0]["id"] == str(replacement["id"])
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM memory_embeddings WHERE project_id=:p"), {"p": own}
                )
                == 3
            )


def test_index_rechecks_fact_after_inference_and_rejects_invalid_vectors(tmp_path: Path) -> None:
    gateway = owner()
    settings = configured()
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        fact = publish(gateway, own, capture("Copper customers renew annually"))

        def withdraw(texts: list[str], purpose: str) -> list[list[float]]:
            gateway.transition_memory(
                own, fact["id"], MemoryTransition(action="withdraw", reason="race fixture")
            )
            return [[1.0, 0.0]]

        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=withdraw):
            assert index_project(gateway, settings, own, limit=16) == {"indexed": 0, "skipped_changed": 1}
        gateway.transition_memory(own, fact["id"], MemoryTransition(action="restore", reason="fixture"))
        for vector in [[0.0, 0.0], [float("nan"), 1.0], [float("inf"), 1.0], [1.0]]:
            with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", return_value=[vector]):
                with pytest.raises(DBAPIError):
                    index_project(gateway, settings, own, limit=16)
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM memory_embeddings WHERE project_id=:p"), {"p": own}
                )
                == 0
            )


def test_publication_during_query_cannot_return_incomplete_results(tmp_path: Path) -> None:
    gateway = owner()
    settings = configured()
    search = ToolGateway(gateway.engine, "operator", live_text_settings=settings)
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        publish(gateway, own, capture("Copper customers renew annually"))
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            index_project(gateway, settings, own, limit=16)

        def publish_during_query(texts: list[str], purpose: str) -> list[list[float]]:
            publish(gateway, own, capture("Another current approved fact"))
            return vectors(texts, purpose)

        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=publish_during_query):
            with pytest.raises(Unavailable, match="index_incomplete"):
                search.retrieve_memory(own, "subscription extension", 5, "project")


def test_hybrid_approval_is_immediately_searchable_without_manual_index(tmp_path: Path) -> None:
    gateway = owner()
    hybrid = ToolGateway(gateway.engine, "operator", live_text_settings=configured())
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            fact = publish(hybrid, own, capture("Copper customers renew annually"))
            assert [
                item["id"] for item in hybrid.retrieve_memory(own, "subscription extension", 5, "project")
            ] == [fact["id"]]
            assert index_project(gateway, configured(), own, limit=16)["indexed"] == 0


def test_failed_publication_embedding_preserves_pending_candidate_and_current_fact(tmp_path: Path) -> None:
    gateway = owner()
    hybrid = ToolGateway(gateway.engine, "operator", live_text_settings=configured())
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            fact = publish(hybrid, own, capture("Copper customers renew annually"))
        saved = hybrid.capture_company_memory(
            own, capture("Copper customers renew monthly", replaces_memory_id=fact["id"])
        )
        candidate = saved["candidate"]["id"]
        with patch(
            "onefoundry.memory_embeddings.LocalEmbeddings.embed",
            side_effect=Unavailable("memory_embedding_timeout"),
        ):
            with pytest.raises(Unavailable, match="memory_embedding_timeout"):
                hybrid.review_candidate(own, candidate, CandidateReview(decision="approved"))
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM dreamcycle_candidates WHERE project_id=:p AND id=:id"),
                    {"p": own, "id": candidate},
                )
                == "pending"
            )
            assert (
                connection.scalar(
                    text("SELECT state FROM memory_items WHERE project_id=:p AND id=:id"),
                    {"p": own, "id": fact["id"]},
                )
                == "current"
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM memory_items WHERE project_id=:p"), {"p": own})
                == 1
            )
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            hybrid.review_candidate(own, candidate, CandidateReview(decision="approved"))
            assert len(hybrid.retrieve_memory(own, "Copper", 5, "project")) == 1


def test_restoring_memory_refreshes_missing_current_model_embedding(tmp_path: Path) -> None:
    gateway = owner()
    hybrid = ToolGateway(gateway.engine, "operator", live_text_settings=configured())
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        fact = publish(gateway, own, capture("Copper customers renew annually"))
        hybrid.transition_memory(own, fact["id"], MemoryTransition(action="withdraw", reason="fixture"))
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            hybrid.transition_memory(own, fact["id"], MemoryTransition(action="restore", reason="fixture"))
            assert hybrid.retrieve_memory(own, "subscription extension", 5, "project")[0]["id"] == fact["id"]


def test_publication_and_vector_are_atomic_and_duplicate_review_cannot_reembed(tmp_path: Path) -> None:
    gateway = owner()
    hybrid = ToolGateway(gateway.engine, "operator", live_text_settings=configured())
    entered, release = Event(), Event()

    def embedding(texts: list[str], purpose: str) -> list[list[float]]:
        entered.set()
        assert release.wait(10), "publication observer did not release the fixture"
        return vectors(texts, purpose)

    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        saved = hybrid.capture_company_memory(own, capture("Copper customers renew annually"))

        def approve() -> str:
            try:
                hybrid.review_candidate(own, saved["candidate"]["id"], CandidateReview(decision="approved"))
                return "approved"
            except Conflict:
                return "conflict"

        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=embedding) as call:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(approve)
                try:
                    assert entered.wait(10)
                    assert hybrid.retrieve_memory(own, "subscription extension", 5, "project") == []
                    second = executor.submit(approve)
                finally:
                    release.set()
                assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [
                    "approved",
                    "conflict",
                ]
            assert call.call_count == 1
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM memory_embeddings WHERE project_id=:p"), {"p": own}
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p "
                        "AND event_type='memory_embeddings_indexed'"
                    ),
                    {"p": own},
                )
                == 1
            )


def test_rejection_lexical_publication_and_foreign_candidate_do_not_embed(tmp_path: Path) -> None:
    gateway = owner()
    hybrid = ToolGateway(gateway.engine, "operator", live_text_settings=configured())
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own, foreign = project(client), project(client)
        saved = hybrid.capture_company_memory(own, capture("Copper customers renew annually"))
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed") as call:
            with pytest.raises(NotFound):
                hybrid.review_candidate(
                    foreign, saved["candidate"]["id"], CandidateReview(decision="approved")
                )
            hybrid.review_candidate(own, saved["candidate"]["id"], CandidateReview(decision="rejected"))
            publish(gateway, own, capture("Lexical mode remains compatible"))
            call.assert_not_called()
