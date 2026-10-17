"""Project-owned raw media and durable, idempotent local render jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0077_video_edit_jobs"
down_revision = "0076_unlimited_prompt_context"
branch_labels = None
depends_on = None
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "video_edit_assets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", UUID),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.CheckConstraint("kind IN ('image','video','audio')"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 67108864"),
        sa.CheckConstraint("length(provenance) BETWEEN 1 AND 240"),
    )
    op.create_table(
        "video_edit_jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("profile_id", UUID, nullable=False),
        sa.Column("request_key", UUID, nullable=False),
        sa.Column("recipe", postgresql.JSONB(), nullable=False),
        sa.Column("recipe_sha256", sa.Text(), nullable=False),
        sa.Column("assets", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("artifact_id", UUID),
        sa.Column("receipt", postgresql.JSONB()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "work_order_id", "request_key"),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "artifact_id"],
            ["workspace_artifacts.project_id", "workspace_artifacts.run_id", "workspace_artifacts.id"],
        ),
        sa.CheckConstraint("recipe_sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("octet_length(recipe::text) <= 65536"),
        sa.CheckConstraint("attempt BETWEEN 0 AND 3"),
        sa.CheckConstraint(
            "status IN ('queued','running','cancel_requested','done','failed','canceled','unknown')"
        ),
        sa.CheckConstraint("(status='done') = (artifact_id IS NOT NULL AND receipt IS NOT NULL)"),
    )
    op.execute(
        "CREATE TRIGGER video_edit_assets_immutable BEFORE UPDATE OR DELETE ON video_edit_assets "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM video_edit_jobs) OR EXISTS(SELECT 1 FROM video_edit_assets)")
    ):
        raise RuntimeError("media edit evidence exists; downgrade would discard receipts")
    op.drop_table("video_edit_jobs")
    op.drop_table("video_edit_assets")
