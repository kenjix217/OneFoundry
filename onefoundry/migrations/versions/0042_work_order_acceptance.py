"""Declare deliverables, review waits, and same-project child order lineage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_work_order_acceptance"
down_revision = "0041_external_action_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_orders",
        sa.Column("deliverable_kind", sa.Text(), nullable=False, server_default="operator_review"),
    )
    op.add_column(
        "work_orders",
        sa.Column(
            "acceptance_checks",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("work_orders", sa.Column("parent_work_order_id", postgresql.UUID(as_uuid=True)))
    op.add_column("work_orders", sa.Column("source_run_id", postgresql.UUID(as_uuid=True)))
    op.create_check_constraint(
        "work_orders_deliverable_kind_valid",
        "work_orders",
        "deliverable_kind IN ('text_reply','artifact','document','code_change',"
        "'external_action','operator_review')",
    )
    op.create_check_constraint(
        "work_orders_acceptance_checks_array",
        "work_orders",
        "jsonb_typeof(acceptance_checks) = 'array' AND jsonb_array_length(acceptance_checks) <= 8",
    )
    op.create_check_constraint(
        "work_orders_parent_not_self",
        "work_orders",
        "parent_work_order_id IS NULL OR parent_work_order_id <> id",
    )
    op.create_foreign_key(
        "work_orders_parent_same_project",
        "work_orders",
        "work_orders",
        ["project_id", "parent_work_order_id"],
        ["project_id", "id"],
    )
    op.create_foreign_key(
        "work_orders_source_run_same_project",
        "work_orders",
        "runs",
        ["project_id", "source_run_id"],
        ["project_id", "id"],
    )
    op.create_index("work_orders_parent_idx", "work_orders", ["project_id", "parent_work_order_id"])
    op.drop_constraint("work_orders_status_valid", "work_orders", type_="check")
    op.create_check_constraint(
        "work_orders_status_valid",
        "work_orders",
        "status IN ('draft','ready','running','blocked','needs_review','done','failed','unknown')",
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM work_orders WHERE status='needs_review')")):
        raise RuntimeError("review waits exist; downgrade would discard their state")
    op.drop_constraint("work_orders_status_valid", "work_orders", type_="check")
    op.create_check_constraint(
        "work_orders_status_valid",
        "work_orders",
        "status IN ('draft','ready','running','blocked','done','failed','unknown')",
    )
    op.drop_index("work_orders_parent_idx", table_name="work_orders")
    op.drop_constraint("work_orders_source_run_same_project", "work_orders", type_="foreignkey")
    op.drop_constraint("work_orders_parent_same_project", "work_orders", type_="foreignkey")
    op.drop_constraint("work_orders_parent_not_self", "work_orders", type_="check")
    op.drop_constraint("work_orders_acceptance_checks_array", "work_orders", type_="check")
    op.drop_constraint("work_orders_deliverable_kind_valid", "work_orders", type_="check")
    op.drop_column("work_orders", "source_run_id")
    op.drop_column("work_orders", "parent_work_order_id")
    op.drop_column("work_orders", "acceptance_checks")
    op.drop_column("work_orders", "deliverable_kind")
