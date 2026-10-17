"""Represent unlimited prompt size without rewriting historical budget policies."""

import sqlalchemy as sa
from alembic import op

revision = "0076_unlimited_prompt_context"
down_revision = "0075_team_context_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "onefoundry_budget_snapshots", "context_bytes_limit", existing_type=sa.Integer(), nullable=True
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM onefoundry_budget_snapshots WHERE context_bytes_limit IS NULL)")
    ):
        raise RuntimeError("unlimited prompt policies exist; downgrade would change their contract")
    op.alter_column(
        "onefoundry_budget_snapshots", "context_bytes_limit", existing_type=sa.Integer(), nullable=False
    )
