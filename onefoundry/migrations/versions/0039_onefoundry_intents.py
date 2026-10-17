"""Durable draft continuation intents for qualified OneFoundry districts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039_onefoundry_intents"
down_revision = "0038_onefoundry_manual"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "onefoundry_intents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("workflow_id", UUID, nullable=False),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("transition_revision", sa.Integer(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text()),
        sa.Column("work_order_id", UUID),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id", "workflow_id"],
            [
                "onefoundry_workflows.project_id",
                "onefoundry_workflows.organization_id",
                "onefoundry_workflows.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.UniqueConstraint("project_id", "id", name="onefoundry_intents_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "workflow_id", "transition_revision", "step_key", name="onefoundry_intent_once"
        ),
        sa.CheckConstraint("transition_revision >= 1", name="onefoundry_intent_revision_valid"),
        sa.CheckConstraint("step_key='draft_outreach'", name="onefoundry_intent_step_valid"),
        sa.CheckConstraint(
            "state IN ('pending','leased','done','blocked','unknown')", name="onefoundry_intent_state_valid"
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 10", name="onefoundry_intent_attempts_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="onefoundry_intent_actor_nonblank"),
    )
    op.create_index(
        "onefoundry_intents_due_idx", "onefoundry_intents", ["state", "due_at", "lease_until", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("onefoundry_intents_due_idx", table_name="onefoundry_intents")
    op.drop_table("onefoundry_intents")
