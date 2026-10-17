"""Advertised result fields match the assignment's existing evidence owner."""

from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.task_session_report_contract import ReportInputError, correction, validate
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway


def report_spec(kind: str) -> dict[str, Any]:
    tools = TaskSessionTools(Mock(spec=ToolGateway), uuid4(), uuid4(), uuid4())
    scope = {
        "kind": "child",
        "is_project_chief": False,
        "actionable": True,
        "allowed_tools": [],
        "role_tools": [],
        "assignment": {"status": "active", "acceptance_kind": kind},
    }
    with patch.object(tools, "_scope", return_value=scope):
        specs = tools.specifications()[0]["tools"]
    return next(dict(item) for item in specs if item["name"] == "report_result")


@pytest.mark.parametrize(
    ("kind", "fields", "required"),
    [
        ("text_report", {"body"}, {"body"}),
        ("artifact", {"body", "artifact_id", "artifact_run_id"}, {"body", "artifact_id", "artifact_run_id"}),
        ("qa_review", {"body", "verdict"}, {"body", "verdict"}),
    ],
)
def test_result_schema_exposes_only_its_evidence_fields(
    kind: str, fields: set[str], required: set[str]
) -> None:
    schema = report_spec(kind)["inputSchema"]
    assert set(schema["properties"]) == fields
    assert set(schema["required"]) == required
    assert schema["additionalProperties"] is False


def test_text_report_instructions_do_not_invite_a_qa_verdict() -> None:
    description = report_spec("text_report")["description"]
    assert "body only" in description
    assert "QA" in description


def test_qa_schema_names_both_independent_review_verdicts() -> None:
    assert report_spec("qa_review")["inputSchema"]["properties"]["verdict"]["enum"] == [
        "passed",
        "needs_rework",
    ]


def test_valid_reports_and_legacy_explicit_nulls_preserve_wire_contract() -> None:
    result = validate(
        "text_report",
        {"body": "Verified fixture source.", "artifact_id": None, "artifact_run_id": None, "verdict": None},
    )
    assert result.body == "Verified fixture source." and result.verdict is None
    artifact_id, run_id = uuid4(), uuid4()
    artifact = validate(
        "artifact",
        {"body": "Saved fixture.", "artifact_id": str(artifact_id), "artifact_run_id": str(run_id)},
    )
    assert (artifact.artifact_id, artifact.artifact_run_id) == (artifact_id, run_id)
    qa = validate("qa_review", {"body": "Independent review.", "verdict": "needs_rework"})
    assert qa.verdict == "needs_rework"
    legacy_qa = validate(
        "qa_review", {"body": "Reviewed dependency.", "verdict": "passed", "artifact_id": str(artifact_id)}
    )
    assert legacy_qa.artifact_id == artifact_id


@pytest.mark.parametrize("field", ["artifact_id", "artifact_run_id", "verdict"])
def test_text_report_rejects_other_evidence_without_echoing_values(field: str) -> None:
    value = "passed" if field == "verdict" else str(uuid4())
    with pytest.raises(ReportInputError) as caught:
        validate("text_report", {"body": "Fixture report.", field: value})
    response = correction(caught.value.code)
    assert response["reason"] == "text_report_body_only"
    assert "body only" in response["next_step"]
    assert value not in response["next_step"]


@pytest.mark.parametrize("missing", ["artifact_id", "artifact_run_id"])
def test_artifact_report_requires_both_saved_ids(missing: str) -> None:
    arguments = {"body": "Fixture artifact.", "artifact_id": str(uuid4()), "artifact_run_id": str(uuid4())}
    arguments.pop(missing)
    with pytest.raises(ReportInputError, match="artifact_id and artifact_run_id"):
        validate("artifact", arguments)


def test_qa_requires_an_explicit_verdict() -> None:
    with pytest.raises(ReportInputError) as caught:
        validate("qa_review", {"body": "Fixture review."})
    assert caught.value.code == "qa_report_requires_verdict"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"body": ""},
        {"body": "a" * 12001},
        {"body": "Fixture.", "verdict": "maybe"},
        {"body": "Fixture.", "artifact_id": "bad-uuid"},
        {"body": "Fixture.", "unexpected": "value"},
    ],
)
def test_malformed_report_boundary_is_rejected(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validate("text_report", arguments)


def test_unadmitted_result_kind_and_unknown_failure_keep_identity() -> None:
    with pytest.raises(ValueError, match="no admitted report"):
        validate("operator_review", {"body": "Fixture."})
    assert correction("tool_target_missing") == {"reason": "tool_target_missing"}
