"""Project-owned signed webhook subscriptions and immutable delivery receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0089_extension_webhooks"
down_revision = "0088_extension_worker_waits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "extension_webhooks",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("credential_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "name"),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_webhook_deliveries",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("webhook_id", uuid, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("signal_id", uuid),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["project_id", "webhook_id"], ["extension_webhooks.project_id", "extension_webhooks.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "signal_id"], ["coordination_signals.project_id", "coordination_signals.id"]
        ),
        sa.UniqueConstraint("project_id", "webhook_id", "delivery_id"),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.execute("""CREATE FUNCTION protect_extension_webhook_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'extension webhook deliveries are immutable'; END; $$""")
    op.execute(
        "CREATE TRIGGER protect_extension_webhook_delivery_change BEFORE UPDATE OR DELETE "
        "ON extension_webhook_deliveries FOR EACH ROW EXECUTE FUNCTION protect_extension_webhook_delivery()"
    )


def downgrade() -> None:
    for name in ("extension_webhook_deliveries", "extension_webhooks"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")):  # noqa: S608
            raise RuntimeError("webhook evidence exists; downgrade is unsafe")
    op.execute("DROP TRIGGER protect_extension_webhook_delivery_change ON extension_webhook_deliveries")
    op.execute("DROP FUNCTION protect_extension_webhook_delivery()")
    op.drop_table("extension_webhook_deliveries")
    op.drop_table("extension_webhooks")
