"""Private text artifact metadata with one-way publication state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_workspace_artifacts"
down_revision = "0030_remote_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.UniqueConstraint("project_id", "run_id", "id", name="workspace_artifacts_scope_unique"),
        sa.UniqueConstraint("project_id", "run_id", "call_id", name="workspace_artifacts_call_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="workspace_artifacts_actor_nonblank"),
        sa.CheckConstraint("call_id ~ '^[A-Za-z0-9_-]{1,128}$'", name="workspace_artifacts_call_valid"),
        sa.CheckConstraint(
            "(kind = 'text' AND mime_type = 'text/plain') OR "
            "(kind = 'markdown' AND mime_type = 'text/markdown') OR "
            "(kind = 'csv' AND mime_type = 'text/csv')",
            name="workspace_artifacts_type_valid",
        ),
        sa.CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 200", name="workspace_artifacts_path_bounded"
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="workspace_artifacts_digest_valid"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 65536", name="workspace_artifacts_size_valid"),
        sa.CheckConstraint(
            "status IN ('pending','ready','unknown')", name="workspace_artifacts_status_valid"
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND finished_at IS NULL) OR "
            "(status IN ('ready','unknown') AND finished_at IS NOT NULL)",
            name="workspace_artifacts_finish_valid",
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_workspace_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.id, NEW.project_id, NEW.run_id, NEW.actor_id, NEW.call_id,
                 NEW.kind, NEW.mime_type, NEW.relative_path, NEW.sha256,
                 NEW.byte_size, NEW.created_at)
             IS DISTINCT FROM
             ROW(OLD.id, OLD.project_id, OLD.run_id, OLD.actor_id, OLD.call_id,
                 OLD.kind, OLD.mime_type, OLD.relative_path, OLD.sha256,
                 OLD.byte_size, OLD.created_at) THEN
            RAISE EXCEPTION 'workspace artifact identity is immutable';
          END IF;
          IF OLD.status <> 'pending' OR NEW.status NOT IN ('ready','unknown') THEN
            RAISE EXCEPTION 'workspace artifact outcome is immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER workspace_artifact_protect BEFORE UPDATE ON workspace_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION protect_workspace_artifact()"
    )
    op.execute(
        "CREATE TRIGGER workspace_artifact_no_delete BEFORE DELETE ON workspace_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER workspace_artifact_no_delete ON workspace_artifacts")
    op.execute("DROP TRIGGER workspace_artifact_protect ON workspace_artifacts")
    op.execute("DROP FUNCTION protect_workspace_artifact()")
    op.drop_table("workspace_artifacts")
