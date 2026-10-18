"""Admit exact, scoped native reads of operator-attached input text."""

import sqlalchemy as sa
from alembic import op

revision = "0094_input_file_pages"
down_revision = "0093_extension_attempt_clock"
branch_labels = None
depends_on = None

TOOL = "onefoundry.read_input_file"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conrelid='task_session_tool_calls'::regclass "
            "AND conname='task_session_input_page_retired'"
        )
    ):
        op.drop_constraint("task_session_input_page_retired", "task_session_tool_calls", type_="check")
    original = connection.scalar(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='task_session_tool_calls'::regclass "
            "AND conname='task_session_tool_calls_name_valid'"
        )
    )
    if not isinstance(original, str) or not original.startswith("CHECK ("):
        raise RuntimeError("native tool name contract unavailable")
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        f"({original[7:-1]}) OR tool_name='{TOOL}'",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM task_session_tool_calls WHERE tool_name=:tool)"),
        {"tool": TOOL},
    ):
        raise RuntimeError("Input file page receipts exist; restore a checkpoint to downgrade")
    op.create_check_constraint(
        "task_session_input_page_retired", "task_session_tool_calls", f"tool_name<>'{TOOL}'"
    )
