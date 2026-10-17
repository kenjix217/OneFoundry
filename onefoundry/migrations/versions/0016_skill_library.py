"""Project-scoped immutable skill procedures and explicit profile bindings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_skill_library"
down_revision = "0015_work_order_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("project_id", "id", name="skills_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "name", name="skills_project_name_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="skills_actor_nonblank"),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name="skills_name_nonblank"),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id", "skill_id"], ["skills.project_id", "skills.id"]),
        sa.UniqueConstraint("project_id", "skill_id", "version", name="skill_versions_exact_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="skill_versions_actor_nonblank"),
        sa.CheckConstraint("version > 0", name="skill_versions_version_positive"),
        sa.CheckConstraint("content ~ '[^[:space:]]'", name="skill_versions_content_nonblank"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="skill_versions_digest_valid"),
    )
    op.execute(
        "CREATE TRIGGER skill_versions_immutable BEFORE UPDATE OR DELETE "
        "ON skill_versions FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )
    op.create_table(
        "profile_skill_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "skill_id", "version"],
            ["skill_versions.project_id", "skill_versions.skill_id", "skill_versions.version"],
        ),
        sa.UniqueConstraint("project_id", "profile_id", "skill_id", name="profile_skill_bindings_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="profile_skill_bindings_actor_nonblank"),
    )
    op.create_table(
        "run_skill_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transition_index", sa.Integer(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_name", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "skill_id", "version"],
            ["skill_versions.project_id", "skill_versions.skill_id", "skill_versions.version"],
        ),
        sa.UniqueConstraint(
            "project_id", "run_id", "transition_index", "skill_id", name="run_skill_snapshots_unique"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="run_skill_snapshots_actor_nonblank"),
        sa.CheckConstraint("transition_index >= 0", name="run_skill_snapshots_index_valid"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="run_skill_snapshots_digest_valid"),
    )
    op.execute(
        "CREATE TRIGGER run_skill_snapshots_immutable BEFORE UPDATE OR DELETE "
        "ON run_skill_snapshots FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER run_skill_snapshots_immutable ON run_skill_snapshots")
    op.drop_table("run_skill_snapshots")
    op.drop_table("profile_skill_bindings")
    op.execute("DROP TRIGGER skill_versions_immutable ON skill_versions")
    op.drop_table("skill_versions")
    op.drop_table("skills")
