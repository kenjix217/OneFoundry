"""Signals, operator rules, and one general coordination intent queue.

The qualified-organization continuation from 0039 moves into the general queue
as the built-in ``onefoundry_draft_outreach`` action; its rows are kept.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043_coordination_core"
down_revision = "0042_work_order_acceptance"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "coordination_signals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("fields", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("origin_work_order_id", UUID),
        sa.Column("origin_run_id", UUID),
        sa.Column("chain_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.Text(), nullable=False, server_default="new"),
        sa.Column("reason", sa.Text()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="coordination_signals_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "source", "dedupe_key", name="coordination_signal_once"),
        sa.ForeignKeyConstraint(
            ["project_id", "origin_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "origin_run_id"], ["runs.project_id", "runs.id"]),
        sa.CheckConstraint(
            "source IN ('internal','schedule','email','webhook','channel','agent_finding','monitor')",
            name="coordination_signal_source_valid",
        ),
        sa.CheckConstraint("signal_type ~ '^[a-z_]+\\.[a-z_]+$'", name="coordination_signal_type_valid"),
        sa.CheckConstraint("length(dedupe_key) BETWEEN 1 AND 300", name="coordination_signal_dedupe_valid"),
        sa.CheckConstraint(
            "jsonb_typeof(fields)='object' AND pg_column_size(fields) <= 8192",
            name="coordination_signal_fields_bounded",
        ),
        sa.CheckConstraint("chain_depth BETWEEN 0 AND 30", name="coordination_signal_depth_valid"),
        sa.CheckConstraint(
            "state IN ('new','matched','ignored','blocked')", name="coordination_signal_state_valid"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="coordination_signal_actor_nonblank"),
    )
    op.create_index("coordination_signals_new_idx", "coordination_signals", ["state", "created_at"])
    op.create_index("coordination_signals_project_idx", "coordination_signals", ["project_id", "created_at"])

    op.create_table(
        "coordination_rules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="coordination_rules_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "name", name="coordination_rule_name_unique"),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 120", name="coordination_rule_name_valid"),
        sa.CheckConstraint("current_version >= 1 AND version >= 1", name="coordination_rule_versions_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="coordination_rule_actor_nonblank"),
    )
    op.create_table(
        "coordination_rule_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("rule_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("filters", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("target_profile_id", UUID, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("deliverable_kind", sa.Text(), nullable=False),
        sa.Column("acceptance_checks", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("autonomy", sa.Text(), nullable=False),
        sa.Column("max_per_day", sa.Integer(), nullable=False),
        sa.Column("max_chain_depth", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "rule_id", "version", name="coordination_rule_version_once"),
        sa.ForeignKeyConstraint(
            ["project_id", "rule_id"], ["coordination_rules.project_id", "coordination_rules.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "target_profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.CheckConstraint("version >= 1", name="coordination_rule_version_positive"),
        sa.CheckConstraint("signal_type ~ '^[a-z_]+\\.[a-z_]+$'", name="coordination_rule_type_valid"),
        sa.CheckConstraint(
            "jsonb_typeof(filters)='object' AND pg_column_size(filters) <= 4096",
            name="coordination_rule_filters_bounded",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(acceptance_checks)='array' AND jsonb_array_length(acceptance_checks) <= 8",
            name="coordination_rule_checks_bounded",
        ),
        sa.CheckConstraint(
            "deliverable_kind IN ('text_reply','artifact','document','code_change',"
            "'external_action','operator_review')",
            name="coordination_rule_deliverable_valid",
        ),
        sa.CheckConstraint("autonomy IN ('draft','ready')", name="coordination_rule_autonomy_valid"),
        sa.CheckConstraint("max_per_day BETWEEN 1 AND 1000", name="coordination_rule_daily_valid"),
        sa.CheckConstraint(
            "max_chain_depth IS NULL OR max_chain_depth BETWEEN 1 AND 30",
            name="coordination_rule_depth_valid",
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="coordination_rule_version_actor_nonblank"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_coordination_rule_version_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'coordination rule versions are immutable';
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER coordination_rule_versions_immutable BEFORE UPDATE OR DELETE "
        "ON coordination_rule_versions FOR EACH ROW "
        "EXECUTE FUNCTION reject_coordination_rule_version_mutation()"
    )

    op.create_table(
        "coordination_controls",
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("version >= 1", name="coordination_controls_version_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="coordination_controls_actor_nonblank"),
    )

    # Generalize the 0039 queue in place so pending continuations survive.
    op.rename_table("onefoundry_intents", "coordination_intents")
    op.execute("ALTER INDEX onefoundry_intents_due_idx RENAME TO coordination_intents_due_idx")
    op.add_column(
        "coordination_intents",
        sa.Column("action_kind", sa.Text(), nullable=False, server_default="onefoundry_draft_outreach"),
    )
    for name, column in (
        ("signal_id", UUID),
        ("rule_id", UUID),
        ("rule_version", sa.Integer()),
        ("target_profile_id", UUID),
        ("autonomy", sa.Text()),
        ("chain_depth", sa.Integer()),
        ("chain_depth_limit", sa.Integer()),
    ):
        op.add_column("coordination_intents", sa.Column(name, column))
    for name in ("workflow_id", "organization_id", "transition_revision"):
        op.alter_column("coordination_intents", name, nullable=True)
    op.drop_constraint("onefoundry_intent_step_valid", "coordination_intents", type_="check")
    op.drop_constraint("onefoundry_intent_revision_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid",
        "coordination_intents",
        "(action_kind='onefoundry_draft_outreach' AND step_key='draft_outreach' "
        "AND workflow_id IS NOT NULL AND organization_id IS NOT NULL AND transition_revision >= 1) OR "
        "(action_kind='rule_work_order' AND step_key='rule_work_order' AND signal_id IS NOT NULL "
        "AND rule_id IS NOT NULL AND rule_version >= 1 AND target_profile_id IS NOT NULL "
        "AND autonomy IN ('draft','ready') AND chain_depth BETWEEN 1 AND 30 "
        "AND chain_depth_limit BETWEEN 1 AND 30)",
    )
    op.create_foreign_key(
        "coordination_intents_signal_same_project",
        "coordination_intents",
        "coordination_signals",
        ["project_id", "signal_id"],
        ["project_id", "id"],
    )
    op.create_foreign_key(
        "coordination_intents_rule_version",
        "coordination_intents",
        "coordination_rule_versions",
        ["project_id", "rule_id", "rule_version"],
        ["project_id", "rule_id", "version"],
    )
    op.create_unique_constraint(
        "coordination_intent_rule_once", "coordination_intents", ["project_id", "signal_id", "rule_id"]
    )

    op.add_column("work_orders", sa.Column("origin_signal_id", UUID))
    op.add_column("work_orders", sa.Column("chain_depth", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("work_orders_chain_depth_valid", "work_orders", "chain_depth BETWEEN 0 AND 30")
    op.create_foreign_key(
        "work_orders_origin_signal_same_project",
        "work_orders",
        "coordination_signals",
        ["project_id", "origin_signal_id"],
        ["project_id", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM coordination_intents "
            "WHERE action_kind<>'onefoundry_draft_outreach')"
        )
    ):
        raise RuntimeError("rule intents exist; downgrade would discard coordination state")
    op.drop_constraint("work_orders_origin_signal_same_project", "work_orders", type_="foreignkey")
    op.drop_constraint("work_orders_chain_depth_valid", "work_orders", type_="check")
    op.drop_column("work_orders", "chain_depth")
    op.drop_column("work_orders", "origin_signal_id")
    op.drop_constraint("coordination_intent_rule_once", "coordination_intents", type_="unique")
    op.drop_constraint("coordination_intents_rule_version", "coordination_intents", type_="foreignkey")
    op.drop_constraint("coordination_intents_signal_same_project", "coordination_intents", type_="foreignkey")
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    for name in ("workflow_id", "organization_id", "transition_revision"):
        op.alter_column("coordination_intents", name, nullable=False)
    for name in (
        "chain_depth_limit",
        "chain_depth",
        "autonomy",
        "target_profile_id",
        "rule_version",
        "rule_id",
        "signal_id",
        "action_kind",
    ):
        op.drop_column("coordination_intents", name)
    op.create_check_constraint(
        "onefoundry_intent_revision_valid", "coordination_intents", "transition_revision >= 1"
    )
    op.create_check_constraint(
        "onefoundry_intent_step_valid", "coordination_intents", "step_key='draft_outreach'"
    )
    op.execute("ALTER INDEX coordination_intents_due_idx RENAME TO onefoundry_intents_due_idx")
    op.rename_table("coordination_intents", "onefoundry_intents")
    op.drop_table("coordination_controls")
    op.execute("DROP TRIGGER coordination_rule_versions_immutable ON coordination_rule_versions")
    op.execute("DROP FUNCTION reject_coordination_rule_version_mutation()")
    op.drop_table("coordination_rule_versions")
    op.drop_table("coordination_rules")
    op.drop_index("coordination_signals_project_idx", table_name="coordination_signals")
    op.drop_index("coordination_signals_new_idx", table_name="coordination_signals")
    op.drop_table("coordination_signals")
