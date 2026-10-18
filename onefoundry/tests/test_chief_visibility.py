"""Chief oversight is read-only and independent of work-order lifecycle state."""

import hashlib
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.chief_inspection_pages import InspectionPageArgs, inspection_page
from onefoundry.chief_session_inspection import SessionInspectionArgs
from onefoundry.chief_work_order_inspection import WorkOrderInspectionArgs
from onefoundry.gateway_errors import Conflict
from onefoundry.task_session_dependency_context import TeamContextBudgetExceeded
from onefoundry.task_session_tools import TaskSessionTools

READS = {
    "onefoundry.read_project_status",
    "onefoundry.read_work_order",
    "onefoundry.read_agent_session",
}


@pytest.mark.parametrize("actionable", [True, False])
def test_canonical_main_chief_can_inspect_without_action_permission(actionable: bool) -> None:
    scope = {
        "actionable": actionable,
        "kind": "main",
        "is_project_chief": True,
        "allowed_tools": [],
        "role_tools": [],
        "assignment": None,
    }
    admitted = TaskSessionTools._admitted(scope)
    assert READS <= admitted
    if not actionable:
        assert "onefoundry.delegate_agent" not in admitted
        assert "onefoundry.submit_completion" not in admitted


@pytest.mark.parametrize("kind,chief", [("child", True), ("main", False), ("child", False)])
def test_oversight_does_not_expand_specialist_permissions(kind: str, chief: bool) -> None:
    scope = {
        "actionable": True,
        "kind": kind,
        "is_project_chief": chief,
        "allowed_tools": [],
        "role_tools": [],
        "assignment": None,
    }
    assert not READS & TaskSessionTools._admitted(scope)


def test_exact_unicode_pages_and_secret_filtering_keep_all_public_records() -> None:
    document = {
        "records": [{"body": "café 中文 saved result. " * 100}],
        "status": "failed",
        "token": "sk-" + "x" * 50,
    }
    pieces: list[str] = []
    offset = 0
    digest = None
    while True:
        result = inspection_page(
            document, InspectionPageArgs(content_offset=offset, expected_sha256=digest), 700, 4
        )
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 700
        pieces.append(result["content"])
        if result["next_content_offset"] is None:
            assert result["next_offset"] == 4
            break
        assert result["next_offset"] is None
        digest = result["body_sha256"]
        offset = result["next_content_offset"]
    body = "".join(pieces)
    assert result["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    saved = json.loads(body)
    assert saved["records"] == document["records"]
    assert saved["status"] == "failed"
    assert saved["token"] == "[held: sensitive content]"


def test_empty_boundary_changed_state_and_invalid_reads_are_explicit() -> None:
    page = inspection_page({"records": []}, InspectionPageArgs(), 1000, None)
    assert json.loads(page["content"])["records"] == []
    assert page["next_content_offset"] is page["next_offset"] is None
    with pytest.raises(Conflict, match="changed"):
        inspection_page(
            {"records": ["new"]}, InspectionPageArgs(expected_sha256=page["body_sha256"]), 1000, None
        )
    for arguments in (
        InspectionPageArgs(content_offset=1),
        InspectionPageArgs(content_offset=10000, expected_sha256=page["body_sha256"]),
    ):
        with pytest.raises(ValueError):
            inspection_page({"records": []}, arguments, 1000, None)
    with pytest.raises(TeamContextBudgetExceeded):
        inspection_page({"records": []}, InspectionPageArgs(), 20, None)
    for model, invalid_arguments in (
        (InspectionPageArgs, {"offset": -1}),
        (InspectionPageArgs, {"unknown": "not admitted"}),
        (WorkOrderInspectionArgs, {"work_order_id": "invalid"}),
        (WorkOrderInspectionArgs, {"work_order_id": uuid4(), "section": "credentials"}),
        (SessionInspectionArgs, {"session_id": uuid4(), "section": "private_reasoning"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid_arguments)


@pytest.mark.parametrize(
    "name",
    sorted(
        READS | {"onefoundry.read_dependency_result", "onefoundry.read_input_file", "onefoundry.list_agents"}
    ),
)
def test_interrupted_reads_reconcile_without_inventing_a_result(name: str) -> None:
    tools = TaskSessionTools.__new__(TaskSessionTools)
    connection = Mock()
    assert tools._readback_action(connection, uuid4(), name) is None
    connection.execute.assert_not_called()
