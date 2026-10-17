"""Persist exact admitted team tool calls and their bounded outcomes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065_team_tool_calls"
down_revision = "0064_team_decision_causes"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "task_session_tool_calls",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("attempt_id", UUID, nullable=False),
        sa.Column("provider_call_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="started"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "id", name="task_session_tool_calls_project_id_unique"),
        sa.UniqueConstraint(
            "project_id",
            "attempt_id",
            "provider_call_id",
            name="task_session_tool_calls_provider_call_unique",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "attempt_id"],
            ["task_session_attempts.project_id", "task_session_attempts.id"],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_session_tool_calls_actor_valid"),
        sa.CheckConstraint(
            "tool_name IN ('onefoundry.delegate_agent','onefoundry.report_result',"
            "'onefoundry.publish_update')",
            name="task_session_tool_calls_name_valid",
        ),
        sa.CheckConstraint(
            "length(provider_call_id) BETWEEN 1 AND 200", name="task_session_tool_calls_id_valid"
        ),
        sa.CheckConstraint("arguments_sha256 ~ '^[0-9a-f]{64}$'", name="task_session_tool_calls_hash_valid"),
        sa.CheckConstraint(
            "status IN ('started','done','failed')", name="task_session_tool_calls_status_valid"
        ),
        sa.CheckConstraint(
            "(status='started' AND finished_at IS NULL AND result IS NULL AND reason IS NULL) OR "
            "(status='done' AND finished_at IS NOT NULL AND result IS NOT NULL AND reason IS NULL) OR "
            "(status='failed' AND finished_at IS NOT NULL AND result IS NULL AND reason IS NOT NULL)",
            name="task_session_tool_calls_outcome_valid",
        ),
    )
    op.create_index(
        "task_session_tool_calls_timeline",
        "task_session_tool_calls",
        ["project_id", "session_id", "created_at"],
    )
    op.execute(
        """CREATE FUNCTION guard_task_session_tool_call() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' OR OLD.status<>'started' OR
             NEW.status NOT IN ('done','failed') OR
             to_jsonb(NEW) - 'status' - 'result' - 'reason' - 'finished_at' IS DISTINCT FROM
             to_jsonb(OLD) - 'status' - 'result' - 'reason' - 'finished_at' THEN
            RAISE EXCEPTION 'team tool call is immutable after settlement';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.execute(
        "CREATE TRIGGER guard_task_session_tool_call_change BEFORE UPDATE OR DELETE "
        "ON task_session_tool_calls FOR EACH ROW EXECUTE FUNCTION guard_task_session_tool_call()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM task_session_tool_calls)")):
        raise RuntimeError("team tool calls exist; downgrade would discard action receipts")
    op.execute("DROP TRIGGER guard_task_session_tool_call_change ON task_session_tool_calls")
    op.execute("DROP FUNCTION guard_task_session_tool_call()")
    op.drop_index("task_session_tool_calls_timeline", table_name="task_session_tool_calls")
    op.drop_table("task_session_tool_calls")
