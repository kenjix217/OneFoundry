"""Text-only scripted SDK runs and cited role outputs.

Revision ID: 0007_synthetic_text
Revises: 0006_candidate_integrity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_synthetic_text"
down_revision = "0006_candidate_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("runs_mode_manual", "runs", type_="check")
    op.create_check_constraint("runs_mode_valid", "runs", "mode IN ('manual_planning', 'synthetic_text')")
    op.create_unique_constraint("memory_items_project_id_id_unique", "memory_items", ["project_id", "id"])
    op.create_table(
        "text_outputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), server_default="synthetic_scripted_sdk", nullable=False),
        sa.Column("configured_model", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "id", name="text_outputs_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "run_id", "role", name="text_outputs_run_role_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="text_outputs_actor_nonblank"),
        sa.CheckConstraint("role IN ('Researcher', 'GTM')", name="text_outputs_role_valid"),
        sa.CheckConstraint("source_kind = 'synthetic_scripted_sdk'", name="text_outputs_source_valid"),
        sa.CheckConstraint("text ~ '[^[:space:]]'", name="text_outputs_text_nonblank"),
        sa.CheckConstraint("octet_length(text) <= 16000", name="text_outputs_text_bounded"),
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="text_outputs_digest_valid"),
        sa.CheckConstraint("tokens_used = 0 AND cost_usd = 0", name="text_outputs_no_provider_cost"),
    )
    op.create_table(
        "text_output_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("text_output_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "text_output_id"], ["text_outputs.project_id", "text_outputs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_project_id", "memory_item_id"], ["memory_items.project_id", "memory_items.id"]
        ),
        sa.UniqueConstraint("text_output_id", "memory_item_id", name="text_output_sources_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="text_output_sources_actor_nonblank"),
    )
    op.execute(
        """
        CREATE FUNCTION validate_text_output_source() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM memory_items m
            WHERE m.project_id = NEW.source_project_id AND m.id = NEW.memory_item_id
              AND (m.project_id = NEW.project_id OR m.scope = 'company')
          ) THEN
            RAISE EXCEPTION 'text output source is not visible to project'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER text_output_source_visibility BEFORE INSERT ON text_output_sources "
        "FOR EACH ROW EXECUTE FUNCTION validate_text_output_source()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER text_output_source_visibility ON text_output_sources")
    op.execute("DROP FUNCTION validate_text_output_source()")
    op.drop_table("text_output_sources")
    op.drop_table("text_outputs")
    op.drop_constraint("memory_items_project_id_id_unique", "memory_items", type_="unique")
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint("runs_mode_manual", "runs", "mode = 'manual_planning'")
