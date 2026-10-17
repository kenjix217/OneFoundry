"""Admit task-bound setup and release preparation while preserving historic receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0083_capability_setup"
down_revision = "0082_zoho_mail"
branch_labels = None
depends_on = None

TOOLS = ("onefoundry.propose_capability_setup", "onefoundry.prepare_extension_release")


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conrelid='task_session_tool_calls'::regclass "
            "AND conname='task_session_setup_retired'"
        )
    ):
        op.drop_constraint("task_session_setup_retired", "task_session_tool_calls", type_="check")
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
        f"({original[7:-1]}) OR tool_name IN ('{TOOLS[0]}','{TOOLS[1]}')",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM task_session_tool_calls WHERE tool_name IN (:setup,:release))"),
        {"setup": TOOLS[0], "release": TOOLS[1]},
    ):
        raise RuntimeError("Setup receipts exist; restore a checkpoint to downgrade")
    op.create_check_constraint(
        "task_session_setup_retired",
        "task_session_tool_calls",
        f"tool_name NOT IN ('{TOOLS[0]}','{TOOLS[1]}')",
    )
