"""Reversible conversation controls and immutable inspection-only turn purpose."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0079_chief_command_workspace"
down_revision = "0078_chief_inspection_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chief_conversation_controls",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("completion_policy", sa.Text(), nullable=False, server_default="operator_review"),
        sa.PrimaryKeyConstraint("project_id", "work_order_id"),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.CheckConstraint("version>=1", name="chief_conversation_version_valid"),
        sa.CheckConstraint(
            "completion_policy IN ('operator_review','chief_verified')",
            name="chief_conversation_completion_valid",
        ),
    )
    op.add_column(
        "task_session_attempts", sa.Column("purpose", sa.Text(), nullable=False, server_default="work")
    )
    op.create_check_constraint(
        "task_session_attempt_purpose_valid", "task_session_attempts", "purpose IN ('work','discussion')"
    )
    op.execute("""
        CREATE FUNCTION protect_team_turn_purpose() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.purpose IS DISTINCT FROM OLD.purpose THEN
            RAISE EXCEPTION 'team turn purpose is immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER team_turn_purpose_immutable BEFORE UPDATE ON task_session_attempts
        FOR EACH ROW EXECUTE FUNCTION protect_team_turn_purpose();
    """)
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        "tool_name IN ('onefoundry.delegate_agent','onefoundry.report_result',"
        "'onefoundry.publish_update','onefoundry.role_tool','onefoundry.submit_completion',"
        "'onefoundry.cancel_assignment','onefoundry.read_dependency_result','onefoundry.list_agents',"
        "'onefoundry.read_project_status','onefoundry.read_work_order','onefoundry.read_agent_session',"
        "'onefoundry.retry_assignment')",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM chief_conversation_controls) OR "
            "EXISTS(SELECT 1 FROM task_session_attempts WHERE purpose='discussion') OR "
            "EXISTS(SELECT 1 FROM task_session_tool_calls WHERE tool_name='onefoundry.retry_assignment')"
        )
    ):
        raise RuntimeError("Chief command records exist; restore a checkpoint to downgrade")
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        "tool_name IN ('onefoundry.delegate_agent','onefoundry.report_result',"
        "'onefoundry.publish_update','onefoundry.role_tool','onefoundry.submit_completion',"
        "'onefoundry.cancel_assignment','onefoundry.read_dependency_result','onefoundry.list_agents',"
        "'onefoundry.read_project_status','onefoundry.read_work_order','onefoundry.read_agent_session')",
    )
    op.execute("DROP TRIGGER team_turn_purpose_immutable ON task_session_attempts")
    op.execute("DROP FUNCTION protect_team_turn_purpose()")
    op.drop_constraint("task_session_attempt_purpose_valid", "task_session_attempts", type_="check")
    op.drop_column("task_session_attempts", "purpose")
    op.drop_table("chief_conversation_controls")
