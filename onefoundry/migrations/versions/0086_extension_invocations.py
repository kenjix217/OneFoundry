"""Exact run capabilities, protected credential refs and one-shot invocation receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0086_extension_invocations"
down_revision = "0085_extension_foundry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "extension_run_tools",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("transition_index", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(64), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("descriptor_sha256", sa.String(64), nullable=False),
        sa.Column("connection_id", uuid),
        sa.Column("connection_version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "connection_id"], ["extension_connections.project_id", "extension_connections.id"]
        ),
        sa.UniqueConstraint("project_id", "run_id", "profile_id", "transition_index", "tool_id"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
        sa.CheckConstraint("package_sha256 ~ '^[0-9a-f]{64}$' AND descriptor_sha256 ~ '^[0-9a-f]{64}$'"),
    )
    op.create_table(
        "extension_connection_credentials",
        sa.Column("project_id", uuid, primary_key=True),
        sa.Column("connection_id", uuid, primary_key=True),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("credential_id", uuid, nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("auth_method", sa.Text(), nullable=False),
        sa.Column("header_name", sa.Text()),
        sa.ForeignKeyConstraint(
            ["project_id", "connection_id"], ["extension_connections.project_id", "extension_connections.id"]
        ),
        sa.CheckConstraint("auth_method IN ('bearer','api_key')"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "extension_invocations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("tool_id", sa.String(64), nullable=False),
        sa.Column("call_id", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("descriptor_sha256", sa.String(64), nullable=False),
        sa.Column("connection_id", uuid),
        sa.Column("connection_version", sa.Integer()),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("approval_id", uuid),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "approval_id"], ["approval_requests.project_id", "approval_requests.id"]
        ),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "run_id", "call_id"),
        sa.UniqueConstraint("project_id", "run_id", "request_sha256"),
        sa.CheckConstraint(
            "status IN ('approval_required','ready','running','done','failed','unknown','cancelled')"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'"),
    )
    op.execute("""CREATE FUNCTION protect_extension_run_tool() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'extension run capabilities are immutable'; END; $$""")
    op.execute(
        "CREATE TRIGGER protect_extension_run_tool_change BEFORE UPDATE OR DELETE "
        "ON extension_run_tools FOR EACH ROW EXECUTE FUNCTION protect_extension_run_tool()"
    )


def downgrade() -> None:
    for name in ("extension_invocations", "extension_run_tools", "extension_connection_credentials"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")):  # noqa: S608 - fixed names
            raise RuntimeError("extension execution evidence exists; downgrade is unsafe")
    op.execute("DROP TRIGGER protect_extension_run_tool_change ON extension_run_tools")
    op.execute("DROP FUNCTION protect_extension_run_tool()")
    op.drop_table("extension_invocations")
    op.drop_table("extension_connection_credentials")
    op.drop_table("extension_run_tools")
