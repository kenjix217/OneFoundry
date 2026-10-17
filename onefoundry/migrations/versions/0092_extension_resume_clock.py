"""Bound interrupted worker resume publication independently of supervisor availability."""

import sqlalchemy as sa
from alembic import op

revision = "0092_extension_resume_clock"
down_revision = "0091_extension_binding_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extension_invocations", sa.Column("worker_resume_started_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM extension_invocations WHERE worker_resume_started_at IS NOT NULL)"
        )
    ):
        raise RuntimeError("worker resume evidence exists; downgrade is unsafe")
    op.drop_column("extension_invocations", "worker_resume_started_at")
