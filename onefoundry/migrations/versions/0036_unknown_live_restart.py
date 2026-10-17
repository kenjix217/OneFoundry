"""Keep each live-text attempt while allowing an explicit attempt after unknown."""

import sqlalchemy as sa
from alembic import op

revision = "0036_unknown_live_restart"
down_revision = "0035_input_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("runs_work_order_id_unique", "runs", type_="unique")
    op.create_index(
        "runs_work_order_running_unique",
        "runs",
        ["work_order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "runs_work_order_latest_idx",
        "runs",
        ["project_id", "work_order_id", sa.text("started_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    duplicate = op.get_bind().scalar(
        sa.text("SELECT 1 FROM runs GROUP BY work_order_id HAVING count(*) > 1 LIMIT 1")
    )
    if duplicate:
        raise RuntimeError("cannot downgrade: work orders have multiple retained runs")
    op.drop_index("runs_work_order_latest_idx", table_name="runs")
    op.drop_index("runs_work_order_running_unique", table_name="runs")
    op.create_unique_constraint("runs_work_order_id_unique", "runs", ["work_order_id"])
