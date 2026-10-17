"""Persist the operator's project brief."""

import sqlalchemy as sa
from alembic import op

revision = "0022_project_brief"
down_revision = "0021_external_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("goal", sa.Text(), nullable=False, server_default=""))
    op.add_column("projects", sa.Column("desired_outcome", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("projects", "desired_outcome")
    op.drop_column("projects", "goal")
