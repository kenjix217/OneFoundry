"""Base project identity and immutable event ledger.

Revision ID: 0001_base
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_base"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name="projects_name_nonblank"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="projects_actor_id_nonblank"),
    )
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="events_actor_id_nonblank"),
        sa.CheckConstraint("event_type ~ '[^[:space:]]'", name="events_event_type_nonblank"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'events are append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_append_only
        BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_no_truncate
        BEFORE TRUNCATE ON events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER events_no_truncate ON events")
    op.execute("DROP TRIGGER events_append_only ON events")
    op.execute("DROP FUNCTION reject_event_mutation()")
    op.drop_table("events")
    op.drop_table("projects")
