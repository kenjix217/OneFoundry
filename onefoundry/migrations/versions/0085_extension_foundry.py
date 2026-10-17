"""Project-scoped inert extensions, connection configuration, and reviewed skills."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0085_extension_foundry"
down_revision = "0084_zoho_oauth_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "extension_packages",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("package_key", sa.String(80), nullable=False),
        sa.Column("package_version", sa.String(80), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("archive", postgresql.BYTEA(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "package_key", "package_version"),
        sa.CheckConstraint("state IN ('draft','active','revoked')"),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("octet_length(archive) BETWEEN 1 AND 4194304"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_connections",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "package_id", "template_id", "label"),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("octet_length(config::text) <= 4096"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_drafts",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "required_tools", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("package_id", uuid),
        sa.Column("source_run_id", uuid),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("accepted_skill_id", uuid),
        sa.Column("accepted_skill_version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "source_run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "accepted_skill_id", "accepted_skill_version"],
            ["skill_versions.project_id", "skill_versions.skill_id", "skill_versions.version"],
        ),
        sa.UniqueConstraint("project_id", "id"),
        sa.CheckConstraint("kind = 'skill'"),
        sa.CheckConstraint("status IN ('pending','accepted','rejected')"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("octet_length(content) <= 16000"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_skill_prerequisites",
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("skill_id", uuid, nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("tool_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "skill_id", "skill_version"],
            ["skill_versions.project_id", "skill_versions.skill_id", "skill_versions.version"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.PrimaryKeyConstraint("project_id", "skill_id", "skill_version", "tool_id"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_package_skills",
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("skill_key", sa.String(64), nullable=False),
        sa.Column("skill_id", uuid, nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "skill_id", "skill_version"],
            ["skill_versions.project_id", "skill_versions.skill_id", "skill_versions.version"],
        ),
        sa.PrimaryKeyConstraint("project_id", "package_id", "skill_key"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_review_bindings",
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "approval_id"], ["approval_requests.project_id", "approval_requests.id"]
        ),
        sa.PrimaryKeyConstraint("project_id", "approval_id"),
        sa.UniqueConstraint("project_id", "package_id", "action", "expected_revision"),
        sa.CheckConstraint("action IN ('activate','revoke')"),
        sa.CheckConstraint("expected_sha256 ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("expected_revision >= 1"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM extension_packages)")):
        raise RuntimeError("extensions exist; restore a checkpoint to downgrade")
    op.drop_table("extension_review_bindings")
    op.drop_table("extension_package_skills")
    op.drop_table("extension_skill_prerequisites")
    op.drop_table("extension_drafts")
    op.drop_table("extension_connections")
    op.drop_table("extension_packages")
