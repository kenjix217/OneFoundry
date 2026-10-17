"""Local operator results, reviewable candidates, and shared memory.

Revision ID: 0005_memory_dreamcycle
Revises: 0004_workflow_links
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_memory_dreamcycle"
down_revision = "0004_workflow_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), server_default="operator_entered", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.UniqueConstraint("project_id", "id", name="run_results_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "run_id", name="run_results_project_run_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="run_results_actor_nonblank"),
        sa.CheckConstraint("summary ~ '[^[:space:]]'", name="run_results_summary_nonblank"),
        sa.CheckConstraint("octet_length(summary) <= 4096", name="run_results_summary_bounded"),
        sa.CheckConstraint("scope IN ('project', 'company')", name="run_results_scope_valid"),
        sa.CheckConstraint("source_kind = 'operator_entered'", name="run_results_source_kind_valid"),
    )
    op.create_table(
        "dreamcycle_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_actor_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id", "source_result_id"], ["run_results.project_id", "run_results.id"]
        ),
        sa.UniqueConstraint("project_id", "id", name="dreamcycle_candidates_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "id", "source_result_id", name="dreamcycle_candidates_result_link_unique"
        ),
        sa.UniqueConstraint("source_result_id", name="dreamcycle_candidates_source_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="dreamcycle_candidates_actor_nonblank"),
        sa.CheckConstraint("summary ~ '[^[:space:]]'", name="dreamcycle_candidates_summary_nonblank"),
        sa.CheckConstraint("octet_length(summary) <= 4096", name="dreamcycle_candidates_summary_bounded"),
        sa.CheckConstraint("scope IN ('project', 'company')", name="dreamcycle_candidates_scope_valid"),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL AND reviewed_by_actor_id IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewed_at IS NOT NULL AND "
            "reviewed_by_actor_id ~ '[^[:space:]]')",
            name="dreamcycle_candidates_review_valid",
        ),
    )
    op.create_table(
        "memory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "source_result_id"], ["run_results.project_id", "run_results.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_candidate_id", "source_result_id"],
            [
                "dreamcycle_candidates.project_id",
                "dreamcycle_candidates.id",
                "dreamcycle_candidates.source_result_id",
            ],
            name="memory_items_candidate_result_fk",
        ),
        sa.UniqueConstraint("source_candidate_id", name="memory_items_candidate_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="memory_items_actor_nonblank"),
        sa.CheckConstraint("scope IN ('project', 'company')", name="memory_items_scope_valid"),
        sa.CheckConstraint("text ~ '[^[:space:]]'", name="memory_items_text_nonblank"),
        sa.CheckConstraint("octet_length(text) <= 4096", name="memory_items_text_bounded"),
    )
    op.create_index("memory_items_visibility_idx", "memory_items", ["scope", "project_id", "created_at"])
    op.execute(
        """
        CREATE FUNCTION validate_memory_publication() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM dreamcycle_candidates c
            WHERE c.project_id = NEW.project_id
              AND c.id = NEW.source_candidate_id
              AND c.source_result_id = NEW.source_result_id
              AND c.status = 'approved'
              AND c.scope = NEW.scope
              AND c.summary = NEW.text
          ) THEN
            RAISE EXCEPTION 'memory publication requires matching approved candidate'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER memory_publication_guard BEFORE INSERT ON memory_items "
        "FOR EACH ROW EXECUTE FUNCTION validate_memory_publication()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER memory_publication_guard ON memory_items")
    op.execute("DROP FUNCTION validate_memory_publication()")
    op.drop_index("memory_items_visibility_idx", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_table("dreamcycle_candidates")
    op.drop_table("run_results")
