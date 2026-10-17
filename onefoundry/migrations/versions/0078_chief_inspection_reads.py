"""Allow exact receipts for read-only canonical Chief project oversight."""

import sqlalchemy as sa
from alembic import op

revision = "0078_chief_inspection_reads"
down_revision = "0077_video_edit_jobs"
branch_labels = None
depends_on = None

PREVIOUS_NAMES = (
    "onefoundry.delegate_agent",
    "onefoundry.report_result",
    "onefoundry.publish_update",
    "onefoundry.role_tool",
    "onefoundry.submit_completion",
    "onefoundry.cancel_assignment",
    "onefoundry.read_dependency_result",
    "onefoundry.list_agents",
)
READ_NAMES = (
    "onefoundry.read_project_status",
    "onefoundry.read_work_order",
    "onefoundry.read_agent_session",
)


def _names(names: tuple[str, ...]) -> None:
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        "tool_name IN (" + ",".join(repr(item) for item in names) + ")",
    )


def upgrade() -> None:
    _names(PREVIOUS_NAMES + READ_NAMES)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM task_session_tool_calls WHERE tool_name IN "
            "('onefoundry.read_project_status','onefoundry.read_work_order','onefoundry.read_agent_session'))"
        )
    ):
        raise RuntimeError("Chief inspection receipts exist; restore a saved checkpoint to downgrade")
    _names(PREVIOUS_NAMES)
