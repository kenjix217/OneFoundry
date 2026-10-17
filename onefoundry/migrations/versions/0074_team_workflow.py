"""Admit receipt-backed role actions, QA verdicts, cancellation and completion."""

import sqlalchemy as sa
from alembic import op

revision = "0074_team_workflow"
down_revision = "0073_memory_capture_lock"
branch_labels = None
depends_on = None

NAMES = (
    "onefoundry.delegate_agent",
    "onefoundry.report_result",
    "onefoundry.publish_update",
    "onefoundry.role_tool",
    "onefoundry.cancel_assignment",
    "onefoundry.submit_completion",
)


def upgrade() -> None:
    op.drop_constraint("workspace_artifacts_type_valid", "workspace_artifacts", type_="check")
    op.create_check_constraint(
        "workspace_artifacts_type_valid",
        "workspace_artifacts",
        "(kind='text' AND mime_type='text/plain') OR (kind='markdown' AND mime_type='text/markdown') OR "
        "(kind='csv' AND mime_type='text/csv') OR (kind='video' AND mime_type='video/webm')",
    )
    op.drop_constraint("workspace_artifacts_size_valid", "workspace_artifacts", type_="check")
    op.create_check_constraint(
        "workspace_artifacts_size_valid",
        "workspace_artifacts",
        "(kind='video' AND byte_size BETWEEN 1 AND 67108864) OR "
        "(kind<>'video' AND byte_size BETWEEN 1 AND 65536)",
    )
    op.execute("""CREATE FUNCTION guard_task_session_mode() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.execution_mode IS DISTINCT FROM OLD.execution_mode THEN
        IF OLD.execution_mode<>'legacy' OR NEW.execution_mode<>'team' OR OLD.kind<>'main' THEN
          RAISE EXCEPTION 'only a main legacy session can enter team mode';
        END IF;
        IF EXISTS(SELECT 1 FROM task_session_attempts WHERE project_id=OLD.project_id
                  AND root_work_order_id=OLD.root_work_order_id AND status IN ('queued','running')) THEN
          RAISE EXCEPTION 'active task execution blocks mode cutover';
        END IF;
      END IF;
      RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER task_session_mode_cutover BEFORE UPDATE OF execution_mode ON task_sessions "
        "FOR EACH ROW EXECUTE FUNCTION guard_task_session_mode()"
    )
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        "tool_name IN (" + ",".join(repr(item) for item in NAMES) + ")",
    )
    op.drop_constraint(
        "task_session_assignments_acceptance_kind_valid", "task_session_assignments", type_="check"
    )
    op.create_check_constraint(
        "task_session_assignments_acceptance_kind_valid",
        "task_session_assignments",
        "acceptance_kind IN ('text_report','artifact','operator_review','qa_review')",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM workspace_artifacts WHERE kind='video') OR "
            "EXISTS(SELECT 1 FROM task_session_assignments WHERE acceptance_kind='qa_review') "
            "OR EXISTS(SELECT 1 FROM task_session_tool_calls WHERE tool_name NOT IN "
            "('onefoundry.delegate_agent','onefoundry.report_result','onefoundry.publish_update'))"
        )
    ):
        raise RuntimeError("team workflow evidence exists; downgrade would remove its contract")
    op.drop_constraint("workspace_artifacts_type_valid", "workspace_artifacts", type_="check")
    op.create_check_constraint(
        "workspace_artifacts_type_valid",
        "workspace_artifacts",
        "(kind='text' AND mime_type='text/plain') OR (kind='markdown' AND mime_type='text/markdown') OR "
        "(kind='csv' AND mime_type='text/csv')",
    )
    op.drop_constraint("workspace_artifacts_size_valid", "workspace_artifacts", type_="check")
    op.create_check_constraint(
        "workspace_artifacts_size_valid", "workspace_artifacts", "byte_size BETWEEN 1 AND 65536"
    )
    op.execute("DROP TRIGGER task_session_mode_cutover ON task_sessions")
    op.execute("DROP FUNCTION guard_task_session_mode()")
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        "tool_name IN ('onefoundry.delegate_agent','onefoundry.report_result','onefoundry.publish_update')",
    )
    op.drop_constraint(
        "task_session_assignments_acceptance_kind_valid", "task_session_assignments", type_="check"
    )
    op.create_check_constraint(
        "task_session_assignments_acceptance_kind_valid",
        "task_session_assignments",
        "acceptance_kind IN ('text_report','artifact','operator_review')",
    )
