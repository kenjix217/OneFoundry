"""Preserve the task and role configuration used by each new run.

Existing runs remain null: their earlier task and profile values are unknown.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_run_audit_snapshots"
down_revision = "0010_synthetic_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("task_title_snapshot", sa.Text()))
    op.add_column("runs", sa.Column("task_instructions_snapshot", sa.Text()))
    op.create_table(
        "run_profile_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("transition_index", sa.Integer(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_name", sa.Text(), nullable=False),
        sa.Column("profile_model", sa.Text()),
        sa.Column("profile_instructions", sa.Text(), nullable=False),
        sa.Column("memory_scope", sa.Text(), nullable=False),
        sa.Column("allowed_tools", postgresql.JSONB(), nullable=False),
        sa.Column("autonomy_level", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint(
            "project_id", "run_id", "transition_index", name="run_profile_snapshots_order_unique"
        ),
        sa.CheckConstraint("transition_index >= 0", name="run_profile_snapshots_index_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="run_profile_snapshots_actor_nonblank"),
        sa.CheckConstraint(
            "memory_scope IN ('company', 'project')", name="run_profile_snapshots_scope_valid"
        ),
        sa.CheckConstraint("jsonb_typeof(allowed_tools) = 'array'", name="run_profile_snapshots_tools_array"),
        sa.CheckConstraint(
            "autonomy_level IN ('manual', 'supervised', 'bounded')",
            name="run_profile_snapshots_autonomy_valid",
        ),
    )
    op.execute(
        "CREATE TRIGGER run_profile_snapshots_immutable BEFORE UPDATE OR DELETE "
        "ON run_profile_snapshots FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER run_profile_snapshots_immutable ON run_profile_snapshots")
    op.drop_table("run_profile_snapshots")
    op.drop_column("runs", "task_instructions_snapshot")
    op.drop_column("runs", "task_title_snapshot")
