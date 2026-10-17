"""Batch child blockers and final branch results before a Chief continuation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0064_team_decision_causes"
down_revision = "0063_team_context_snapshot"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "task_session_decision_causes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("main_session_id", UUID, nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_id", UUID, nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("dispatch_attempt_id", UUID),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("handled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "project_id",
            "root_work_order_id",
            "plan_revision",
            "kind",
            "subject_id",
            name="task_session_decision_causes_unique_cause",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "main_session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "dispatch_attempt_id"],
            ["task_session_attempts.project_id", "task_session_attempts.id"],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_session_decision_causes_actor_valid"),
        sa.CheckConstraint("plan_revision > 0", name="task_session_decision_causes_revision_valid"),
        sa.CheckConstraint("kind IN ('blocker','synthesis')", name="task_session_decision_causes_kind_valid"),
        sa.CheckConstraint(
            "length(detail) BETWEEN 1 AND 160", name="task_session_decision_causes_detail_valid"
        ),
    )
    op.create_index(
        "task_session_decision_causes_pending",
        "task_session_decision_causes",
        ["project_id", "root_work_order_id", "plan_revision", "created_at"],
        postgresql_where=sa.text("dispatch_attempt_id IS NULL AND resolved_at IS NULL"),
    )
    op.execute(
        """CREATE FUNCTION guard_task_session_decision_cause() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' OR OLD.resolved_at IS NOT NULL OR OLD.handled_at IS NOT NULL OR
             to_jsonb(NEW) - 'dispatch_attempt_id' - 'resolved_at' - 'handled_at' IS DISTINCT FROM
             to_jsonb(OLD) - 'dispatch_attempt_id' - 'resolved_at' - 'handled_at' OR
             (OLD.dispatch_attempt_id IS NULL AND
              ((NEW.dispatch_attempt_id IS NULL AND NEW.resolved_at IS NULL) OR
               (NEW.dispatch_attempt_id IS NOT NULL AND NEW.resolved_at IS NOT NULL) OR
               NEW.handled_at IS NOT NULL)) OR
             (OLD.dispatch_attempt_id IS NOT NULL AND
              (NEW.dispatch_attempt_id IS DISTINCT FROM OLD.dispatch_attempt_id OR
               NEW.resolved_at IS NOT NULL OR NEW.handled_at IS NULL)) THEN
            RAISE EXCEPTION 'team decision cause is immutable after dispatch';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.execute(
        "CREATE TRIGGER guard_task_session_decision_cause_change BEFORE UPDATE OR DELETE "
        "ON task_session_decision_causes FOR EACH ROW EXECUTE FUNCTION guard_task_session_decision_cause()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM task_session_decision_causes)")):
        raise RuntimeError("team decision causes exist; downgrade would discard coordination history")
    op.execute("DROP TRIGGER guard_task_session_decision_cause_change ON task_session_decision_causes")
    op.execute("DROP FUNCTION guard_task_session_decision_cause()")
    op.drop_index("task_session_decision_causes_pending", table_name="task_session_decision_causes")
    op.drop_table("task_session_decision_causes")
