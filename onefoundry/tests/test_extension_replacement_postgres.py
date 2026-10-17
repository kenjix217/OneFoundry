"""Independent PG acceptance for failed-request replacement and uncertain effects."""

import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from onefoundry.repository_code_contract import RepositoryCodeTarget
from onefoundry.self_extension_resume import require_resumed
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_runs import bind
from onefoundry.task_session_schemas import SessionMessageCreate
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from tests.test_self_extension_failure_postgres import (
    test_failed_coding_blocks_assignment_and_notifies_chief_once as failed_fixture,
)
from tests.test_self_extension_postgres import payload
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker
from tests.test_team_workflow_postgres import settings_for


def test_replacement_preserves_only_uncertain_or_active_old_blockers(tmp_path: Path) -> None:
    failed_fixture(tmp_path, "candidate")
    oldjob = json.loads(next((tmp_path / "external/jobs").glob("*.json")).read_text())
    (tmp_path / "replacement-settings").mkdir()
    pid = UUID(oldjob["project_id"])
    settings = settings_for(tmp_path / "replacement-settings")
    settings.local_source_root = str(tmp_path / "sources")
    settings.runner_spool_root = str(tmp_path)
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(
        engine,
        settings.operator_actor_id,
        runner_spool_root=str(tmp_path),
        runner_limits=settings.runner_limits,
        runner_receipt_wait_seconds=settings.runner_receipt_wait_seconds,
        live_text_settings=settings,
    )
    try:
        with engine.connect() as c:
            old = c.scalar(
                text(
                    "SELECT payload FROM events WHERE project_id=:p AND "
                    "event_type='self_extension_requested' ORDER BY created_at LIMIT 1"
                ),
                {"p": pid},
            )
        store = TaskSessionStore(gateway)
        view = store.get(pid, UUID(old["original_work_order_id"]))
        store.send(
            pid,
            UUID(old["original_work_order_id"]),
            SessionMessageCreate(
                client_message_id=uuid4(),
                expected_sequence=max(m["sequence"] for m in view["messages"]),
                body="Retry the confirmed failed implementation with a new approved scope.",
            ),
        )
        attempts = TaskSessionAttempts(gateway)
        turn = attempts.claim_next(pid)
        assert turn
        bind(gateway, pid, turn)
        tools = TaskSessionTools(gateway, pid, turn["session_id"], turn["id"])
        target = RepositoryCodeTarget.model_validate(oldjob["target"])
        proposed = payload(
            tools.invoke(
                "onefoundry.propose_maintenance_target", target.model_dump(mode="json"), "replacement-target"
            )
        )
        gateway.decide_approval(pid, UUID(proposed["approval_id"]), ApprovalDecision(decision="approved"))
        replacement = payload(
            tools.invoke(
                "onefoundry.request_extension",
                {
                    "operation": old["operation"],
                    "target_approval_id": proposed["approval_id"],
                    "objective": "Retry confirmed failed bounded fixture.",
                    "acceptance_check": "Verified replacement resumes original.",
                    "checkpoint": "Continue original task.",
                },
                "replacement-request",
            )
        )
        assert replacement["extension_key"] != old["extension_key"]
        with engine.begin() as c:
            gateway._event(
                c,
                pid,
                UUID(old["original_work_order_id"]),
                None,
                "self_extension_resumed",
                {
                    "approval_id": proposed["approval_id"],
                    "original_session_id": replacement["original_session_id"],
                    "extension_key": replacement["extension_key"],
                    "checkpoint": replacement["checkpoint"],
                },
            )
        with engine.connect() as c:
            require_resumed(c, pid, UUID(old["original_work_order_id"]))
    finally:
        engine.dispose()
