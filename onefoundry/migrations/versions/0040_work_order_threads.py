"""Allow several named conversations for the same work order and agent."""

import sqlalchemy as sa
from alembic import op

revision = "0040_work_order_threads"
down_revision = "0039a_onefoundry_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_conversations",
        sa.Column("title", sa.Text(), nullable=False, server_default="Main chat"),
    )
    op.add_column(
        "task_conversations",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_check_constraint(
        "task_conversation_title_valid",
        "task_conversations",
        "length(title) BETWEEN 1 AND 120 AND title ~ '[^[:space:]]'",
    )
    op.drop_constraint("task_conversations_order_profile_unique", "task_conversations", type_="unique")
    op.create_index(
        "task_conversation_active_owner_unique",
        "task_conversations",
        ["project_id", "work_order_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    duplicates = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM task_conversations GROUP BY project_id,work_order_id,profile_id "
            "HAVING count(*) > 1)"
        )
    )
    if duplicates:
        raise RuntimeError("multiple work-order chats exist; downgrade would lose conversation history")
    op.drop_index("task_conversation_active_owner_unique", table_name="task_conversations")
    op.create_unique_constraint(
        "task_conversations_order_profile_unique",
        "task_conversations",
        ["project_id", "work_order_id", "profile_id"],
    )
    op.drop_constraint("task_conversation_title_valid", "task_conversations", type_="check")
    op.drop_column("task_conversations", "is_active")
    op.drop_column("task_conversations", "title")
