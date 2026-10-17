"""Project Chief conversations are independent of a selected work order."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0058_project_chief_inbox"
down_revision = "0057_chief_followup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "remote_inbound", "work_order_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True
    )
    op.add_column("remote_inbound", sa.Column("credential_revision", sa.Integer()))
    op.alter_column(
        "remote_outbound", "work_order_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True
    )
    op.create_table(
        "project_chief_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("remote_inbound_id", postgresql.UUID(as_uuid=True)),
        sa.Column("operator_text", sa.Text(), nullable=False),
        sa.Column("operator_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("assistant_text", sa.Text()),
        sa.Column("assistant_sha256", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("action", sa.Text()),
        sa.Column(
            "candidate_work_order_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("target_work_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_work_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_rule_id", postgresql.UUID(as_uuid=True)),
        sa.Column("account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("provider", sa.Text()),
        sa.Column("model_id", sa.Text()),
        sa.Column("provider_response_id", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "remote_inbound_id"], ["remote_inbound.project_id", "remote_inbound.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "target_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "created_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "created_rule_id"], ["coordination_rules.project_id", "coordination_rules.id"]
        ),
        sa.UniqueConstraint("project_id", "id", name="project_chief_turn_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "client_turn_id", name="project_chief_turn_client_unique"),
        sa.UniqueConstraint("remote_inbound_id", name="project_chief_turn_inbound_unique"),
        sa.CheckConstraint(
            "source_kind IN ('operator_ui','discord','wechat')", name="project_chief_turn_source_valid"
        ),
        sa.CheckConstraint(
            "(source_kind='operator_ui' AND remote_inbound_id IS NULL) OR "
            "(source_kind IN ('discord','wechat') AND remote_inbound_id IS NOT NULL)",
            name="project_chief_turn_origin_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','unknown','blocked')",
            name="project_chief_turn_status_valid",
        ),
        sa.CheckConstraint("operator_sha256 ~ '^[0-9a-f]{64}$'", name="project_chief_turn_digest_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="project_chief_turn_actor_nonblank"),
    )
    op.create_index("project_chief_turns_recent", "project_chief_turns", ["project_id", "created_at", "id"])
    op.create_index("project_chief_turns_pending", "project_chief_turns", ["status", "created_at", "id"])
    op.add_column("remote_outbound", sa.Column("chief_turn_id", postgresql.UUID(as_uuid=True)))
    op.add_column("work_orders", sa.Column("origin_chief_turn_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "work_order_origin_chief_turn_same_project",
        "work_orders",
        "project_chief_turns",
        ["project_id", "origin_chief_turn_id"],
        ["project_id", "id"],
    )
    op.create_foreign_key(
        "remote_outbound_chief_turn_same_project",
        "remote_outbound",
        "project_chief_turns",
        ["project_id", "chief_turn_id"],
        ["project_id", "id"],
    )
    # A channel's confirmed identity survives. Historical work-order conversations remain intact.
    op.execute("UPDATE remote_channels SET work_order_id=NULL")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM project_chief_turns)")):
        raise RuntimeError("project Chief turns exist; downgrade would lose the conversation")
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM remote_inbound WHERE work_order_id IS NULL)")):
        raise RuntimeError(
            "project-scoped inbound receipts exist; downgrade cannot restore work order bindings"
        )
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM remote_outbound WHERE work_order_id IS NULL)")):
        raise RuntimeError(
            "project-scoped outbound receipts exist; downgrade cannot restore work order bindings"
        )
    op.drop_constraint("remote_outbound_chief_turn_same_project", "remote_outbound", type_="foreignkey")
    op.drop_column("remote_outbound", "chief_turn_id")
    op.drop_constraint("work_order_origin_chief_turn_same_project", "work_orders", type_="foreignkey")
    op.drop_column("work_orders", "origin_chief_turn_id")
    op.drop_index("project_chief_turns_pending", table_name="project_chief_turns")
    op.drop_index("project_chief_turns_recent", table_name="project_chief_turns")
    op.drop_table("project_chief_turns")
    op.alter_column(
        "remote_outbound", "work_order_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False
    )
    op.drop_column("remote_inbound", "credential_revision")
    op.alter_column(
        "remote_inbound", "work_order_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False
    )
