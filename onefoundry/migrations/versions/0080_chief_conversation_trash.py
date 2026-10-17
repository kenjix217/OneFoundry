"""Recoverable conversation removal without deleting work, files or events."""

import sqlalchemy as sa
from alembic import op

revision = "0080_chief_conversation_trash"
down_revision = "0079_chief_command_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chief_conversation_controls",
        sa.Column("trashed", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_check_constraint(
        "chief_conversation_trash_held",
        "chief_conversation_controls",
        "NOT trashed OR (archived AND paused)",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM chief_conversation_controls WHERE trashed)")
    ):
        raise RuntimeError("Trashed conversations exist; restore a checkpoint to downgrade")
    op.drop_constraint("chief_conversation_trash_held", "chief_conversation_controls", type_="check")
    op.drop_column("chief_conversation_controls", "trashed")
