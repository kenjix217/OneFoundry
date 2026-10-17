"""Explicit Microsoft sign-out receipts, independent of local account removal."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069_connector_signout"
down_revision = "0068_coordination_rule_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("connector_account_features_array", "connector_accounts", type_="check")
    op.create_check_constraint(
        "connector_account_features_array",
        "connector_accounts",
        "jsonb_typeof(features)='array' AND jsonb_array_length(features)<=4",
    )
    op.create_table(
        "connector_signouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('dispatching','completed','failed','unknown')", name="connector_signout_state_valid"
        ),
    )
    op.create_index("connector_signouts_account", "connector_signouts", ["project_id", "account_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM connector_signouts)")):
        raise RuntimeError("sign-out receipts exist; downgrade would discard them")
    op.drop_table("connector_signouts")
    op.drop_constraint("connector_account_features_array", "connector_accounts", type_="check")
    op.create_check_constraint(
        "connector_account_features_array",
        "connector_accounts",
        "jsonb_typeof(features)='array' AND jsonb_array_length(features)<=3",
    )
