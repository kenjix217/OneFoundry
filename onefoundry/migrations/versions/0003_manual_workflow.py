"""Manual work orders, runs, approvals, and event links.

Revision ID: 0003_manual_workflow
Revises: 0002_profiles
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_manual_workflow"
down_revision = "0002_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("profiles_project_id_id_unique", "profiles", ["project_id", "id"])
    op.create_table(
        "work_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "id", name="work_orders_project_id_id_unique"),
        sa.CheckConstraint("title ~ '[^[:space:]]'", name="work_orders_title_nonblank"),
        sa.CheckConstraint("instructions ~ '[^[:space:]]'", name="work_orders_instructions_nonblank"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="work_orders_actor_nonblank"),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'running', 'done', 'failed', 'unknown')",
            name="work_orders_status_valid",
        ),
    )
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "id", name="runs_project_id_id_unique"),
        sa.UniqueConstraint("work_order_id", name="runs_work_order_id_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="runs_actor_nonblank"),
        sa.CheckConstraint("status IN ('running', 'done', 'failed', 'unknown')", name="runs_status_valid"),
        sa.CheckConstraint("mode = 'manual_planning'", name="runs_mode_manual"),
        sa.CheckConstraint("tokens_used = 0 AND cost_usd = 0", name="runs_no_execution_cost"),
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("proposal_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="approvals_actor_nonblank"),
        sa.CheckConstraint("target ~ '[^[:space:]]'", name="approvals_target_nonblank"),
        sa.CheckConstraint("content ~ '[^[:space:]]'", name="approvals_content_nonblank"),
        sa.CheckConstraint(
            "action_type IN ('email', 'publish', 'merge', 'deploy', 'cloud_change', 'other')",
            name="approvals_action_valid",
        ),
        sa.CheckConstraint("proposal_digest ~ '^[0-9a-f]{64}$'", name="approvals_digest_valid"),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND decided_at IS NOT NULL)",
            name="approvals_decision_valid",
        ),
    )
    op.add_column("events", sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("events", sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "events_project_work_order_fk",
        "events",
        "work_orders",
        ["project_id", "work_order_id"],
        ["project_id", "id"],
    )
    op.create_foreign_key(
        "events_project_run_fk", "events", "runs", ["project_id", "run_id"], ["project_id", "id"]
    )
    op.create_index("events_project_run_time_idx", "events", ["project_id", "run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("events_project_run_time_idx", table_name="events")
    op.drop_constraint("events_project_run_fk", "events", type_="foreignkey")
    op.drop_constraint("events_project_work_order_fk", "events", type_="foreignkey")
    op.drop_column("events", "run_id")
    op.drop_column("events", "work_order_id")
    op.drop_table("approval_requests")
    op.drop_table("runs")
    op.drop_table("work_orders")
    op.drop_constraint("profiles_project_id_id_unique", "profiles", type_="unique")
