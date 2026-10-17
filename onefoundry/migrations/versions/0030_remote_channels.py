"""Project-owned remote channel bindings and one-shot message receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_remote_channels"
down_revision = "0029_task_conversation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reply_policy", sa.Text(), nullable=False, server_default="approval_each"),
        sa.Column("sender_id", sa.Text()),
        sa.Column("chat_id", sa.Text()),
        sa.Column("bot_id", sa.Text()),
        sa.Column("observed_sender_id", sa.Text()),
        sa.Column("observed_chat_id", sa.Text()),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("identity_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True)),
        sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credential_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("connected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("poll_cursor", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "id", name="remote_channels_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "platform", name="remote_channels_project_platform_unique"),
        sa.CheckConstraint("platform IN ('discord','wechat')", name="remote_channels_platform_valid"),
        sa.CheckConstraint(
            "reply_policy IN ('approval_each','bound_dm')", name="remote_channels_reply_policy_valid"
        ),
        sa.CheckConstraint("length(poll_cursor) <= 8192", name="remote_channels_cursor_bounded"),
        sa.CheckConstraint("credential_revision >= 0", name="remote_channels_credential_revision_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="remote_channels_actor_nonblank"),
    )
    op.create_table(
        "remote_pairing_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scanner_id", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "channel_id"], ["remote_channels.project_id", "remote_channels.id"]
        ),
        sa.UniqueConstraint("project_id", "id", name="remote_pairing_project_id_id_unique"),
    )
    op.create_table(
        "remote_inbound",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform_message_id", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "channel_id"], ["remote_channels.project_id", "remote_channels.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.UniqueConstraint("project_id", "id", name="remote_inbound_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "channel_id", "platform_message_id", name="remote_inbound_platform_message_unique"
        ),
        sa.CheckConstraint(
            "status IN ('processing','done','failed','unknown')", name="remote_inbound_status_valid"
        ),
        sa.CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="remote_inbound_digest_valid"),
    )
    op.create_table(
        "remote_outbound",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("channel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbound_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_id", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=False),
        sa.Column("reply_policy", sa.Text(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("remote_message_id", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "channel_id"], ["remote_channels.project_id", "remote_channels.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "inbound_id"], ["remote_inbound.project_id", "remote_inbound.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["approval_id"], ["approval_requests.id"]),
        sa.UniqueConstraint("project_id", "inbound_id", name="remote_outbound_inbound_unique"),
        sa.CheckConstraint(
            "status IN ('approval_pending','pending','claimed','accepted_by_platform','failed','unknown')",
            name="remote_outbound_status_valid",
        ),
        sa.CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="remote_outbound_digest_valid"),
        sa.CheckConstraint(
            "reply_policy IN ('approval_each','bound_dm')", name="remote_outbound_policy_valid"
        ),
    )
    op.create_index("remote_outbound_project_status_idx", "remote_outbound", ["project_id", "status"])
    op.execute(
        "INSERT INTO remote_channels (id,project_id,actor_id,platform) "
        "SELECT gen_random_uuid(),id,actor_id,'discord' FROM projects"
    )
    op.execute("""CREATE FUNCTION seed_default_remote_channel() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          INSERT INTO remote_channels (id,project_id,actor_id,platform)
          VALUES (gen_random_uuid(),NEW.id,NEW.actor_id,'discord');
          RETURN NEW;
        END; $$""")
    op.execute(
        "CREATE TRIGGER projects_default_remote_channel AFTER INSERT ON projects "
        "FOR EACH ROW EXECUTE FUNCTION seed_default_remote_channel()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER projects_default_remote_channel ON projects")
    op.execute("DROP FUNCTION seed_default_remote_channel()")
    op.drop_index("remote_outbound_project_status_idx", table_name="remote_outbound")
    op.drop_table("remote_outbound")
    op.drop_table("remote_inbound")
    op.drop_table("remote_pairing_challenges")
    op.drop_table("remote_channels")
