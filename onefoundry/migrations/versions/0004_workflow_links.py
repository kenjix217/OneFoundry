"""Bind run links to their work order and retain outcome reason outside events.

Revision ID: 0004_workflow_links
Revises: 0003_manual_workflow
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_workflow_links"
down_revision = "0003_manual_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    mismatched_approvals = connection.scalar(
        sa.text(
            "SELECT count(*) FROM approval_requests a JOIN runs r "
            "ON r.project_id = a.project_id AND r.id = a.run_id "
            "WHERE a.work_order_id <> r.work_order_id"
        )
    )
    mismatched_events = connection.scalar(
        sa.text(
            "SELECT count(*) FROM events e JOIN runs r "
            "ON r.project_id = e.project_id AND r.id = e.run_id "
            "WHERE e.work_order_id IS DISTINCT FROM r.work_order_id"
        )
    )
    if mismatched_approvals or mismatched_events:
        raise RuntimeError("existing workflow links must be repaired before migration 0004")

    op.add_column(
        "runs",
        sa.Column("outcome_reason", sa.Text(), server_default="", nullable=False),
    )
    op.create_unique_constraint(
        "runs_project_id_id_work_order_unique",
        "runs",
        ["project_id", "id", "work_order_id"],
    )
    op.drop_constraint("approval_requests_project_id_run_id_fkey", "approval_requests", type_="foreignkey")
    op.create_foreign_key(
        "approval_requests_run_work_order_fk",
        "approval_requests",
        "runs",
        ["project_id", "run_id", "work_order_id"],
        ["project_id", "id", "work_order_id"],
    )
    op.drop_constraint("events_project_run_fk", "events", type_="foreignkey")
    op.create_check_constraint(
        "events_run_requires_work_order", "events", "run_id IS NULL OR work_order_id IS NOT NULL"
    )
    op.create_foreign_key(
        "events_run_work_order_fk",
        "events",
        "runs",
        ["project_id", "run_id", "work_order_id"],
        ["project_id", "id", "work_order_id"],
    )


def downgrade() -> None:
    op.drop_constraint("events_run_work_order_fk", "events", type_="foreignkey")
    op.drop_constraint("events_run_requires_work_order", "events", type_="check")
    op.create_foreign_key(
        "events_project_run_fk", "events", "runs", ["project_id", "run_id"], ["project_id", "id"]
    )
    op.drop_constraint("approval_requests_run_work_order_fk", "approval_requests", type_="foreignkey")
    op.create_foreign_key(
        "approval_requests_project_id_run_id_fkey",
        "approval_requests",
        "runs",
        ["project_id", "run_id"],
        ["project_id", "id"],
    )
    op.drop_constraint("runs_project_id_id_work_order_unique", "runs", type_="unique")
    op.drop_column("runs", "outcome_reason")
