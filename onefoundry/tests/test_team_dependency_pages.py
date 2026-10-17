"""Large confirmed inputs remain exact and available within existing bounds."""

import hashlib
import json
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.task_session_dependency_context import (
    DependencyPageArgs,
    TeamContextBudgetExceeded,
    project_dependencies,
    read_dependency_page,
)


def test_large_artifact_projection_preserves_identity_and_declares_omission() -> None:
    artifact = {
        "artifact_id": str(uuid4()),
        "run_id": str(uuid4()),
        "sha256": "a" * 64,
        "kind": "markdown",
        "content": "Published budget record. " * 1200,
    }
    body = json.dumps({"summary": "Verified public budget arithmetic.", "artifact": artifact})
    item: dict[str, Any] = {
        "assignment_id": uuid4(),
        "message_id": uuid4(),
        "body": body,
        "acceptance_kind": "artifact",
    }
    projected = project_dependencies([item], 1500)
    assert len(projected[0].encode()) <= 1500
    reference = json.loads(projected[0])
    assert reference["context_complete"] is False
    assert reference["artifact"]["sha256"] == artifact["sha256"]
    assert "content" not in reference["artifact"]
    assert reference["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert "read_dependency_result" in reference["next_step"]
    assert item["body"] == body

    # JSON in an ordinary text report is not a verified artifact receipt.
    reference = json.loads(project_dependencies([{**item, "acceptance_kind": "text_report"}], 1500)[0])
    assert "artifact" not in reference


def test_small_plain_result_stays_complete_and_empty_is_empty() -> None:
    item: dict[str, Any] = {"assignment_id": uuid4(), "message_id": uuid4(), "body": "Supported finding."}
    assert "Supported finding." in project_dependencies([item], 1500)[0]
    assert "Supported finding." in project_dependencies([item], None)[0]
    assert project_dependencies([], 1500) == []
    with pytest.raises(TeamContextBudgetExceeded):
        project_dependencies([item], 10)


def test_unicode_pages_reconstruct_every_character_under_serialized_bound() -> None:
    item: dict[str, Any] = {
        "assignment_id": uuid4(),
        "message_id": uuid4(),
        "body": "Evidence café 中文. " * 700,
    }
    connection = Mock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = {"id": uuid4()}
    pages = []
    offset = 0
    with patch("onefoundry.task_session_dependency_context.dependency_results", return_value=[item]):
        while True:
            page = read_dependency_page(
                connection,
                uuid4(),
                uuid4(),
                DependencyPageArgs(assignment_id=item["assignment_id"], offset=offset),
                1024,
            )
            assert len(json.dumps(page, ensure_ascii=False).encode()) <= 1024
            assert page["message_id"] == str(item["message_id"])
            assert page["body_sha256"] == hashlib.sha256(item["body"].encode()).hexdigest()
            pages.append(page["content"])
            if page["next_offset"] is None:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
    assert "".join(pages) == item["body"]


def test_missing_input_invalid_cursor_and_sensitive_input_are_errors() -> None:
    connection = Mock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = None
    with patch("onefoundry.task_session_dependency_context.dependency_results", return_value=[]):
        with pytest.raises(NotFound):
            read_dependency_page(
                connection, uuid4(), uuid4(), DependencyPageArgs(assignment_id=uuid4()), 1024
            )
    for values in (
        {"assignment_id": "bad"},
        {"assignment_id": uuid4(), "offset": -1},
        {"assignment_id": uuid4(), "unknown": True},
    ):
        with pytest.raises(ValidationError):
            DependencyPageArgs.model_validate(values)
    item: dict[str, Any] = {"assignment_id": uuid4(), "message_id": uuid4(), "body": "Plain evidence."}
    with patch("onefoundry.task_session_dependency_context.dependency_results", return_value=[item]):
        with pytest.raises(ValueError, match="offset exceeds"):
            read_dependency_page(
                connection,
                uuid4(),
                uuid4(),
                DependencyPageArgs(assignment_id=item["assignment_id"], offset=1000),
                1024,
            )
    with patch("onefoundry.task_session_dependency_context.contains_sensitive_text", return_value=True):
        with pytest.raises(Conflict, match="sensitive"):
            project_dependencies([item], 1500)


@pytest.mark.parametrize("state", ["pending", "ready", "active", "blocked", "canceled"])
def test_scoped_unfinished_assignment_reports_availability_instead_of_missing(state: str) -> None:
    assignment_id = uuid4()
    connection = Mock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = {
        "id": assignment_id,
        "status": state,
        "child_session_id": uuid4(),
    }
    with patch("onefoundry.task_session_dependency_context.dependency_results", return_value=[]):
        result = read_dependency_page(
            connection, uuid4(), uuid4(), DependencyPageArgs(assignment_id=assignment_id), 1024
        )
    assert result["status"] == ("pending" if state in ("pending", "ready", "active") else state)
    assert result["assignment_status"] == state
    assert result["content_available"] is False
    assert "content" not in result and "message_id" not in result
    assert result["next_step"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 1024


def test_unconfirmed_reads_keep_budget_and_corrupt_result_failures_visible() -> None:
    connection = Mock()
    assignment_id = uuid4()
    row = {"id": assignment_id, "status": "pending", "child_session_id": uuid4()}
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    with patch("onefoundry.task_session_dependency_context.dependency_results", return_value=[]):
        with pytest.raises(TeamContextBudgetExceeded):
            read_dependency_page(
                connection, uuid4(), uuid4(), DependencyPageArgs(assignment_id=assignment_id), 10
            )
        row["status"] = "satisfied"
        with pytest.raises(Conflict, match="reconcile its saved evidence"):
            read_dependency_page(
                connection, uuid4(), uuid4(), DependencyPageArgs(assignment_id=assignment_id), 1024
            )
