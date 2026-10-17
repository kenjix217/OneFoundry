"""Focused bounded synthetic memory policy and HTTP contract checks."""

import hashlib
from typing import Any
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from onefoundry.synthetic_memory import (
    _qa_source,
    _sales_source,
    _source_summary,
    propose_synthetic_candidate,
)
from onefoundry.synthetic_memory_api import make_synthetic_memory_router
from onefoundry.tool_gateway import Conflict


class QueryResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def mappings(self) -> "QueryResult":
        return self

    def one_or_none(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value


def test_sales_requires_confirmed_delivery_and_exact_body_digest() -> None:
    project_id, run_id = uuid4(), uuid4()
    body = "Safe local draft"
    draft = {
        "id": uuid4(),
        "project_id": project_id,
        "run_id": run_id,
        "source_run_id": run_id,
        "actor_id": "profile:sales",
        "subject": "Local preview",
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "delivery_status": "pending",
        "sent_at": None,
        "approval_id": uuid4(),
    }
    connection = Mock()
    connection.execute.return_value = QueryResult(draft)
    with pytest.raises(Conflict, match="confirmed Mailpit delivery"):
        _sales_source(connection, project_id, run_id)
    assert connection.execute.call_count == 1
    draft["delivery_status"] = "delivered"
    draft["sent_at"] = "2026-09-26T10:00:00Z"
    draft["body"] = "changed after digest"
    with pytest.raises(Conflict, match="digest"):
        _sales_source(connection, project_id, run_id)
    assert connection.execute.call_count == 2


def test_qa_requires_verified_done_receipt_and_passing_tests() -> None:
    project_id, run_id = uuid4(), uuid4()
    qa = {
        "id": uuid4(),
        "project_id": project_id,
        "run_id": run_id,
        "actor_id": "operator",
        "status": "done",
        "receipt_sha256": "a" * 64,
        "commit": "b" * 40,
        "diff_sha256": "c" * 64,
        "tests": [{"name": "qa_fixture_test", "status": "failed", "exit_code": 1}],
    }
    connection = Mock()
    connection.execute.return_value = QueryResult(qa)
    with pytest.raises(Conflict, match="passing tests"):
        _qa_source(connection, project_id, run_id)
    qa["tests"] = [{"name": "qa_fixture_test", "status": "passed", "exit_code": 0}]
    assert _qa_source(connection, project_id, run_id)["id"] == qa["id"]
    assert "qa_fixture_test" in _source_summary("synthetic_code", qa)


def test_secret_and_oversize_source_cannot_become_candidate() -> None:
    source = {"subject": "Local preview", "body": "api_key=unsafe"}
    with pytest.raises(ValueError, match="secret"):
        _source_summary("synthetic_text", source)
    source["body"] = "x" * 4096
    with pytest.raises(ValueError, match="byte limit"):
        _source_summary("synthetic_text", source)


def test_invalid_scope_rejected_before_database_access() -> None:
    gateway = MagicMock()
    with pytest.raises(ValueError, match="scope"):
        propose_synthetic_candidate(gateway, uuid4(), uuid4(), "other")
    gateway.engine.begin.assert_not_called()


def test_http_rejects_invalid_scope_without_gateway_write() -> None:
    gateway = MagicMock()
    app = FastAPI()
    app.include_router(make_synthetic_memory_router(gateway))
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{uuid4()}/runs/{uuid4()}/synthetic-memory-candidate",
            json={"scope": "other"},
        )
    assert response.status_code == 422
    gateway.propose_synthetic_candidate.assert_not_called()


def test_proposal_is_one_result_and_one_candidate_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from onefoundry import memory_store, synthetic_memory

    project_id, run_id, result_id, candidate_id, source_id = (uuid4() for _ in range(5))
    run = {"id": run_id, "work_order_id": uuid4(), "status": "done", "mode": "synthetic_text"}
    source = {"id": source_id, "body_sha256": "a" * 64, "subject": "Preview", "body": "Safe local draft"}
    result = {
        "id": result_id,
        "run_id": run_id,
        "source_kind": "synthetic_sales",
        "scope": "project",
        "summary": "Source summary",
        "source_sales_draft_id": source_id,
        "source_sha256": "a" * 64,
    }
    candidate = {"id": candidate_id, "source_result_id": result_id, "status": "pending"}
    connection = Mock()
    connection.execute.return_value = QueryResult(run)
    gateway = MagicMock()
    gateway.engine.begin.return_value.__enter__.return_value = connection
    gateway.actor_id = "operator"
    monkeypatch.setattr(synthetic_memory, "_sales_source", lambda *_: source)
    monkeypatch.setattr(synthetic_memory, "_source_summary", lambda *_: "Source summary")
    get_result = Mock(side_effect=[None, result])
    get_candidate = Mock(side_effect=[None, candidate])
    insert_result = Mock(return_value=result)
    insert_candidate = Mock(return_value=candidate)
    monkeypatch.setattr(memory_store, "get_result_by_run", get_result)
    monkeypatch.setattr(memory_store, "get_candidate_by_result", get_candidate)
    monkeypatch.setattr(memory_store, "insert_synthetic_result", insert_result)
    monkeypatch.setattr(memory_store, "insert_candidate", insert_candidate)
    first = propose_synthetic_candidate(gateway, project_id, run_id, "project")
    second = propose_synthetic_candidate(gateway, project_id, run_id, "project")
    assert first == second == (result, candidate)
    insert_result.assert_called_once()
    insert_candidate.assert_called_once()
    assert gateway._event.call_count == 2
    assert all("Source summary" not in repr(call) for call in gateway._event.call_args_list)


def test_proposal_rejects_foreign_or_non_done_run_before_source_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onefoundry import synthetic_memory
    from onefoundry.tool_gateway import NotFound

    gateway = MagicMock()
    connection = Mock()
    gateway.engine.begin.return_value.__enter__.return_value = connection
    source = Mock()
    monkeypatch.setattr(synthetic_memory, "_sales_source", source)
    project_id, run_id = uuid4(), uuid4()
    connection.execute.return_value = QueryResult(None)
    with pytest.raises(NotFound, match="run not found"):
        propose_synthetic_candidate(gateway, project_id, run_id, "project")
    connection.execute.return_value = QueryResult({"status": "failed", "mode": "synthetic_text"})
    with pytest.raises(Conflict, match="completed synthetic"):
        propose_synthetic_candidate(gateway, project_id, run_id, "project")
    source.assert_not_called()
    gateway._event.assert_not_called()
