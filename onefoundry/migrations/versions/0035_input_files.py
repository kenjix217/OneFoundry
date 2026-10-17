"""Project-owned input originals, extracted chunks, and exact context links."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035_input_files"
down_revision = "0034_merge_model_reasoning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_conversation_turns",
        sa.Column("input_sections_omitted", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "task_turn_input_sections_nonnegative", "task_conversation_turns", "input_sections_omitted >= 0"
    )
    op.create_unique_constraint(
        "task_turns_project_id_id_unique", "task_conversation_turns", ["project_id", "id"]
    )
    op.create_table(
        "input_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("extension", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text()),
        sa.Column("extractor_version", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("project_id", "id", name="input_files_project_id_id_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="input_files_actor_nonblank"),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 255", name="input_files_name_length"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="input_files_digest_valid"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 104857600", name="input_files_size_valid"),
        sa.CheckConstraint(
            "relative_path = project_id::text || '/' || id::text", name="input_files_path_identity"
        ),
        sa.CheckConstraint(
            "status IN ('pending','ready_text','ready_visual','rejected')",
            name="input_files_status_valid",
        ),
        sa.CheckConstraint(
            "(status='pending' AND finished_at IS NULL AND reason_code IS NULL) OR "
            "(status IN ('ready_text','ready_visual') AND finished_at IS NOT NULL "
            "AND reason_code IS NULL AND extractor_version IS NOT NULL) OR "
            "(status='rejected' AND finished_at IS NOT NULL AND reason_code IS NOT NULL)",
            name="input_files_outcome_valid",
        ),
    )
    op.create_index("input_files_project_time_idx", "input_files", ["project_id", "created_at", "id"])
    op.create_table(
        "input_file_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "file_id"], ["input_files.project_id", "input_files.id"]),
        sa.UniqueConstraint("project_id", "file_id", "ordinal", name="input_file_chunks_ordinal_unique"),
        sa.UniqueConstraint("project_id", "id", name="input_file_chunks_project_id_id_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="input_file_chunks_actor_nonblank"),
        sa.CheckConstraint("ordinal >= 0", name="input_file_chunks_ordinal_nonnegative"),
        sa.CheckConstraint("length(source_label) BETWEEN 1 AND 200", name="input_file_chunks_label_valid"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 8192", name="input_file_chunks_content_valid"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="input_file_chunks_digest_valid"),
    )
    op.create_index(
        "input_file_chunks_search_idx",
        "input_file_chunks",
        [sa.text("to_tsvector('simple', content)")],
        postgresql_using="gin",
    )
    op.create_table(
        "input_file_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id", "file_id"], ["input_files.project_id", "input_files.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "turn_id"], ["task_conversation_turns.project_id", "task_conversation_turns.id"]
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="input_file_links_actor_nonblank"),
        sa.CheckConstraint(
            "(scope='project' AND work_order_id IS NULL AND turn_id IS NULL) OR "
            "(scope IN ('work_order','staged') AND work_order_id IS NOT NULL AND turn_id IS NULL) OR "
            "(scope='turn' AND work_order_id IS NOT NULL AND turn_id IS NOT NULL)",
            name="input_file_links_scope_valid",
        ),
    )
    op.create_index("input_file_links_project_idx", "input_file_links", ["project_id", "scope", "file_id"])
    op.create_index("input_file_links_order_idx", "input_file_links", ["project_id", "work_order_id"])
    op.create_index("input_file_links_turn_idx", "input_file_links", ["project_id", "turn_id"])
    op.create_index(
        "input_file_links_project_unique",
        "input_file_links",
        ["project_id", "file_id"],
        unique=True,
        postgresql_where=sa.text("scope='project'"),
    )
    op.create_index(
        "input_file_links_order_unique",
        "input_file_links",
        ["project_id", "work_order_id", "file_id"],
        unique=True,
        postgresql_where=sa.text("scope IN ('work_order','staged')"),
    )
    op.create_index(
        "input_file_links_turn_unique",
        "input_file_links",
        ["project_id", "turn_id", "file_id"],
        unique=True,
        postgresql_where=sa.text("scope='turn'"),
    )
    op.create_table(
        "input_file_context_uses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "turn_id"], ["task_conversation_turns.project_id", "task_conversation_turns.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(["project_id", "file_id"], ["input_files.project_id", "input_files.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "chunk_id"], ["input_file_chunks.project_id", "input_file_chunks.id"]
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="input_file_uses_actor_nonblank"),
        sa.CheckConstraint("(turn_id IS NULL) <> (run_id IS NULL)", name="input_file_uses_owner_valid"),
        sa.CheckConstraint("kind IN ('text','visual')", name="input_file_uses_kind_valid"),
        sa.CheckConstraint("length(source_label) BETWEEN 1 AND 200", name="input_file_uses_label_valid"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="input_file_uses_digest_valid"),
        sa.CheckConstraint("byte_count BETWEEN 1 AND 104857600", name="input_file_uses_size_valid"),
        sa.CheckConstraint(
            "(kind='text' AND chunk_id IS NOT NULL) OR (kind='visual' AND chunk_id IS NULL)",
            name="input_file_uses_chunk_valid",
        ),
    )
    op.create_index("input_file_uses_turn_idx", "input_file_context_uses", ["project_id", "turn_id"])
    op.create_index("input_file_uses_run_idx", "input_file_context_uses", ["project_id", "run_id"])
    op.create_index(
        "input_file_uses_turn_unique",
        "input_file_context_uses",
        ["project_id", "turn_id", "file_id", "chunk_id"],
        unique=True,
        postgresql_where=sa.text("turn_id IS NOT NULL"),
    )
    op.create_index(
        "input_file_uses_run_unique",
        "input_file_context_uses",
        ["project_id", "run_id", "file_id", "chunk_id"],
        unique=True,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("input_file_context_uses")
    op.drop_table("input_file_links")
    op.drop_table("input_file_chunks")
    op.drop_table("input_files")
    op.drop_constraint("task_turns_project_id_id_unique", "task_conversation_turns", type_="unique")
    op.drop_constraint("task_turn_input_sections_nonnegative", "task_conversation_turns", type_="check")
    op.drop_column("task_conversation_turns", "input_sections_omitted")
