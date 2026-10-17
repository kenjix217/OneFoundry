"""Bind each Chief assignment to the decision that created it."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0056_turn_assignments"
down_revision = "0055_chief_assignment_launch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "task_automation_decisions_project_id_id", "task_automation_decisions", ["project_id", "id"]
    )
    op.create_table(
        "task_assignment_links",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", "position"),
        sa.UniqueConstraint("work_order_id", name="task_assignment_one_decision"),
        sa.ForeignKeyConstraint(
            ["project_id", "decision_id"],
            ["task_automation_decisions.project_id", "task_automation_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.CheckConstraint("position BETWEEN 1 AND 50", name="task_assignment_position_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_assignment_actor_nonblank"),
    )
    op.create_index(
        "task_assignment_links_project_decision", "task_assignment_links", ["project_id", "decision_id"]
    )
    op.execute(
        """INSERT INTO task_assignment_links (decision_id,project_id,position,work_order_id,actor_id)
        SELECT id,project_id,1,assigned_work_order_id,actor_id
        FROM task_automation_decisions WHERE assigned_work_order_id IS NOT NULL"""
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM task_assignment_links WHERE position > 1)")
    ):
        raise RuntimeError("team assignments exist; downgrade would lose their turn links")
    op.drop_index("task_assignment_links_project_decision", table_name="task_assignment_links")
    op.drop_table("task_assignment_links")
    op.drop_constraint("task_automation_decisions_project_id_id", "task_automation_decisions", type_="unique")
