"""Admit scoped read tools without changing existing action receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0075_team_context_reads"
down_revision = "0074_team_workflow"
branch_labels = None
depends_on = None

PRIOR_NAMES = (
    "onefoundry.delegate_agent",
    "onefoundry.report_result",
    "onefoundry.publish_update",
    "onefoundry.role_tool",
    "onefoundry.cancel_assignment",
    "onefoundry.submit_completion",
)
READ_NAMES = ("onefoundry.read_dependency_result", "onefoundry.list_agents")


def _constraint(names: tuple[str, ...]) -> None:
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        "tool_name IN (" + ",".join(repr(item) for item in names) + ")",
    )


def upgrade() -> None:
    _constraint(PRIOR_NAMES + READ_NAMES)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM task_session_tool_calls WHERE tool_name IN "
            "('onefoundry.read_dependency_result','onefoundry.list_agents'))"
        )
    ):
        raise RuntimeError("team context read receipts exist; downgrade would remove their contract")
    _constraint(PRIOR_NAMES)
