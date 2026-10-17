"""Record execution start separately from the initial approval proposal."""

import sqlalchemy as sa
from alembic import op

revision = "0093_extension_attempt_clock"
down_revision = "0092_extension_resume_clock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extension_invocations", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE extension_invocations SET started_at=created_at WHERE status='running'")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM extension_invocations WHERE started_at IS NOT NULL)")
    ):
        raise RuntimeError("extension execution evidence exists; downgrade is unsafe")
    op.drop_column("extension_invocations", "started_at")
