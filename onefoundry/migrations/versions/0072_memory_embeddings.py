"""Rebuildable approved-fact vectors inside the existing PostgreSQL store."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0072_memory_embeddings"
down_revision = "0071_company_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_embeddings",
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("model_key", sa.Text(), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Double()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "memory_id"], ["memory_items.project_id", "memory_items.id"]),
        sa.CheckConstraint(
            "model_key ~ '^[0-9a-f]{64}$' AND source_sha256 ~ '^[0-9a-f]{64}$'",
            name="memory_embedding_hashes_valid",
        ),
        sa.CheckConstraint(
            "dimensions BETWEEN 1 AND 4000 AND array_ndims(embedding)=1 "
            "AND array_lower(embedding,1)=1 AND cardinality(embedding)=dimensions "
            "AND array_position(embedding,NULL) IS NULL",
            name="memory_embedding_shape_valid",
        ),
        sa.CheckConstraint(
            "actor_id ~ '[^[:space:]]' AND octet_length(model) BETWEEN 1 AND 960",
            name="memory_embedding_identity_valid",
        ),
    )
    op.create_index("memory_embedding_model_project", "memory_embeddings", ["model_key", "project_id"])
    op.execute("""
        CREATE FUNCTION validate_memory_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE norm double precision;
        BEGIN
          IF NOT ('Infinity'::double precision > ALL(NEW.embedding) AND
                  '-Infinity'::double precision < ALL(NEW.embedding)) THEN
            RAISE EXCEPTION 'memory embedding must be finite' USING ERRCODE='23514';
          END IF;
          SELECT sum(value*value) INTO norm FROM unnest(NEW.embedding) AS value;
          IF norm IS NULL OR abs(norm-1)>0.000001 THEN
            RAISE EXCEPTION 'memory embedding must be normalized' USING ERRCODE='23514';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM memory_items m WHERE m.id=NEW.memory_id
              AND m.project_id=NEW.project_id AND m.state='current'
              AND encode(sha256(convert_to(m.text,'UTF8')),'hex')=NEW.source_sha256) THEN
            RAISE EXCEPTION 'memory embedding requires exact current fact' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END; $$;
        CREATE TRIGGER memory_embedding_guard BEFORE INSERT OR UPDATE ON memory_embeddings
          FOR EACH ROW EXECUTE FUNCTION validate_memory_embedding();
    """)


def downgrade() -> None:
    op.drop_table("memory_embeddings")
    op.execute("DROP FUNCTION validate_memory_embedding()")
