"""A separate versioned project plan for operator and agent updates."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_project_plan"
down_revision = "0053_conversation_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_plans",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="project_plan_actor_nonblank"),
        sa.CheckConstraint("version >= 1", name="project_plan_version_positive"),
        sa.CheckConstraint("length(body) <= 4000", name="project_plan_body_bounded"),
    )
    op.execute("INSERT INTO project_plans (project_id,actor_id) SELECT id,actor_id FROM projects")
    op.add_column("task_automation_decisions", sa.Column("plan_version", sa.Integer()))


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM project_plans WHERE version > 1)")):
        raise RuntimeError("saved project plans exist; downgrade would lose them")
    op.drop_column("task_automation_decisions", "plan_version")
    op.drop_table("project_plans")
