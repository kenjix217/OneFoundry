"""One-shot live text outputs with measured usage and provider cost."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_live_text"
down_revision = "0013_model_transports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode_valid",
        "runs",
        "mode IN ('manual_planning', 'synthetic_text', 'synthetic_code', 'live_text')",
    )
    op.drop_constraint("runs_no_execution_cost", "runs", type_="check")
    op.alter_column("runs", "tokens_used", existing_type=sa.Integer(), nullable=True)
    op.alter_column(
        "runs", "cost_usd", existing_type=sa.Numeric(12, 6), type_=sa.Numeric(20, 10), nullable=True
    )
    op.create_check_constraint(
        "runs_cost_valid",
        "runs",
        "(mode = 'live_text' AND ((status = 'done' AND tokens_used >= 0 AND "
        "(cost_usd IS NULL OR cost_usd >= 0)) OR "
        "(status <> 'done' AND tokens_used IS NULL AND cost_usd IS NULL))) OR "
        "(mode <> 'live_text' AND tokens_used = 0 AND cost_usd = 0)",
    )
    op.create_table(
        "live_text_run_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("prompt_sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.UniqueConstraint("project_id", "run_id", name="live_text_run_bindings_run_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="live_text_run_bindings_actor_nonblank"),
        sa.CheckConstraint(
            "provider IN ('openai','grok','ollama')", name="live_text_run_bindings_provider_valid"
        ),
        sa.CheckConstraint("prompt_sha256 ~ '^[0-9a-f]{64}$'", name="live_text_run_bindings_prompt_digest"),
    )
    op.create_table(
        "live_text_outputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(20, 10)),
        sa.Column("cost_usd_ticks", sa.BigInteger()),
        sa.Column("provider_response_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "run_id", name="live_text_outputs_run_unique"),
        sa.UniqueConstraint("project_id", "id", name="live_text_outputs_project_id_id_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="live_text_outputs_actor_nonblank"),
        sa.CheckConstraint("provider IN ('openai','grok','ollama')", name="live_text_outputs_provider_valid"),
        sa.CheckConstraint("text ~ '[^[:space:]]'", name="live_text_outputs_text_nonblank"),
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="live_text_outputs_digest_valid"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="live_text_outputs_tokens_valid"),
        sa.CheckConstraint(
            "(cost_usd_ticks IS NULL AND cost_usd IS NULL) OR "
            "(provider = 'grok' AND cost_usd_ticks >= 0 AND "
            "cost_usd = cost_usd_ticks::numeric / 10000000000)",
            name="live_text_outputs_cost_measured",
        ),
    )
    op.create_table(
        "live_text_output_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("output_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "output_id"], ["live_text_outputs.project_id", "live_text_outputs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_project_id", "memory_item_id"], ["memory_items.project_id", "memory_items.id"]
        ),
        sa.UniqueConstraint("output_id", "memory_item_id", name="live_text_output_sources_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="live_text_output_sources_actor_nonblank"),
    )
    op.execute(
        """CREATE FUNCTION validate_live_text_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM memory_items m
            WHERE m.project_id = NEW.source_project_id AND m.id = NEW.memory_item_id
              AND (m.project_id = NEW.project_id OR m.scope = 'company')
          ) THEN
            RAISE EXCEPTION 'live text source is not visible to project' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        "CREATE TRIGGER live_text_source_visibility BEFORE INSERT ON live_text_output_sources "
        "FOR EACH ROW EXECUTE FUNCTION validate_live_text_source()"
    )


def downgrade() -> None:
    live_count = op.get_bind().scalar(sa.text("SELECT count(*) FROM runs WHERE mode='live_text'"))
    if live_count:
        raise RuntimeError("remove live text runs before downgrading")
    op.execute("DROP TRIGGER live_text_source_visibility ON live_text_output_sources")
    op.execute("DROP FUNCTION validate_live_text_source()")
    op.drop_table("live_text_output_sources")
    op.drop_table("live_text_outputs")
    op.drop_table("live_text_run_bindings")
    op.drop_constraint("runs_cost_valid", "runs", type_="check")
    op.alter_column(
        "runs", "cost_usd", existing_type=sa.Numeric(20, 10), type_=sa.Numeric(12, 6), nullable=False
    )
    op.alter_column("runs", "tokens_used", existing_type=sa.Integer(), nullable=False)
    op.create_check_constraint("runs_no_execution_cost", "runs", "tokens_used = 0 AND cost_usd = 0")
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode_valid", "runs", "mode IN ('manual_planning', 'synthetic_text', 'synthetic_code')"
    )
