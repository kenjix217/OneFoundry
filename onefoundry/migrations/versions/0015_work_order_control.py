"""Versioned ownership and reversible operator blocks.

Revision ID: 0015_work_order_control
Revises: 0014_live_text
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_work_order_control"
down_revision = "0014_live_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_orders", sa.Column("version", sa.Integer(), server_default="1", nullable=False))
    op.add_column("work_orders", sa.Column("block_reason", sa.Text(), nullable=True))
    op.add_column("work_orders", sa.Column("blocked_from_status", sa.Text(), nullable=True))
    op.create_check_constraint("work_orders_version_positive", "work_orders", "version > 0")
    op.drop_constraint("work_orders_status_valid", "work_orders", type_="check")
    op.create_check_constraint(
        "work_orders_status_valid",
        "work_orders",
        "status IN ('draft', 'ready', 'running', 'blocked', 'done', 'failed', 'unknown')",
    )
    op.create_check_constraint(
        "work_orders_block_valid",
        "work_orders",
        "(status = 'blocked' AND block_reason ~ '[^[:space:]]' "
        "AND blocked_from_status IN ('draft', 'ready', 'running')) OR "
        "(status <> 'blocked' AND block_reason IS NULL AND blocked_from_status IS NULL)",
    )
    op.execute(
        "CREATE FUNCTION work_orders_bump_version() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN NEW.version := OLD.version + 1; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER work_orders_version_on_change BEFORE UPDATE ON work_orders "
        "FOR EACH ROW EXECUTE FUNCTION work_orders_bump_version()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER work_orders_version_on_change ON work_orders")
    op.execute("DROP FUNCTION work_orders_bump_version()")
    op.drop_constraint("work_orders_block_valid", "work_orders", type_="check")
    op.drop_constraint("work_orders_status_valid", "work_orders", type_="check")
    op.create_check_constraint(
        "work_orders_status_valid",
        "work_orders",
        "status IN ('draft', 'ready', 'running', 'done', 'failed', 'unknown')",
    )
    op.drop_constraint("work_orders_version_positive", "work_orders", type_="check")
    op.drop_column("work_orders", "blocked_from_status")
    op.drop_column("work_orders", "block_reason")
    op.drop_column("work_orders", "version")
