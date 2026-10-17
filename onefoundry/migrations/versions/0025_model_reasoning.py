"""Persist discovered Codex reasoning choices and exact text-run selection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_model_reasoning"
down_revision = "0024_source_bound_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_catalog",
        sa.Column(
            "reasoning_efforts", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.create_check_constraint(
        "model_catalog_reasoning_efforts_array", "model_catalog", "jsonb_typeof(reasoning_efforts) = 'array'"
    )
    op.add_column("profiles", sa.Column("reasoning_effort", sa.Text()))
    op.create_check_constraint(
        "profiles_reasoning_effort_valid",
        "profiles",
        "reasoning_effort IS NULL OR reasoning_effort ~ '^[a-z][a-z0-9_]{0,31}$'",
    )
    op.add_column("live_text_run_bindings", sa.Column("reasoning_effort", sa.Text()))
    op.add_column("run_profile_snapshots", sa.Column("profile_reasoning_effort", sa.Text()))


def downgrade() -> None:
    op.drop_column("run_profile_snapshots", "profile_reasoning_effort")
    op.drop_column("live_text_run_bindings", "reasoning_effort")
    op.drop_constraint("profiles_reasoning_effort_valid", "profiles", type_="check")
    op.drop_column("profiles", "reasoning_effort")
    op.drop_constraint("model_catalog_reasoning_efforts_array", "model_catalog", type_="check")
    op.drop_column("model_catalog", "reasoning_efforts")
