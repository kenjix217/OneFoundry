"""Chief of Staff agent proposals, approved through the inbox, within an operator tool ceiling."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_agent_proposals"
down_revision = "0047_coordination_auto_start"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "coordination_controls",
        sa.Column(
            "profile_tool_ceiling", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.create_check_constraint(
        "coordination_controls_ceiling_array",
        "coordination_controls",
        "jsonb_typeof(profile_tool_ceiling)='array' AND jsonb_array_length(profile_tool_ceiling) <= 32",
    )
    op.create_table(
        "coordination_profile_proposals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("approval_id", UUID, nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("proposal_sha256", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("profile_id", UUID),
        sa.Column("first_work_order_id", UUID),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "approval_id", name="coordination_profile_proposal_once"),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "approval_id"], ["approval_requests.project_id", "approval_requests.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "first_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.CheckConstraint(
            "jsonb_typeof(proposal)='object' AND pg_column_size(proposal) <= 24576",
            name="coordination_profile_proposal_bounded",
        ),
        sa.CheckConstraint("proposal_sha256 ~ '^[0-9a-f]{64}$'", name="coordination_profile_proposal_digest"),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected')", name="coordination_profile_proposal_state_valid"
        ),
        sa.CheckConstraint(
            "(state='approved') = (profile_id IS NOT NULL)", name="coordination_profile_proposal_profile_set"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="coordination_profile_proposal_actor_nonblank"),
    )


def downgrade() -> None:
    op.drop_table("coordination_profile_proposals")
    op.drop_constraint("coordination_controls_ceiling_array", "coordination_controls", type_="check")
    op.drop_column("coordination_controls", "profile_tool_ceiling")
