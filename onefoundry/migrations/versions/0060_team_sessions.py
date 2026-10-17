"""Durable work-order team sessions, messages, and bounded attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060_team_sessions"
down_revision = "0059_discord_guild_channel"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "task_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("profile_id", UUID, nullable=False),
        sa.Column("parent_session_id", UUID),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("provider_thread_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="task_sessions_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "root_work_order_id", "id", name="task_sessions_project_root_id_unique"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "parent_session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_sessions_actor_nonblank"),
        sa.CheckConstraint("kind IN ('main','child','consultation')", name="task_sessions_kind_valid"),
        sa.CheckConstraint(
            "status IN ('open','working','waiting_for_agent','waiting_for_operator','closed')",
            name="task_sessions_status_valid",
        ),
        sa.CheckConstraint(
            "(kind='main' AND parent_session_id IS NULL) OR (kind<>'main' AND parent_session_id IS NOT NULL)",
            name="task_sessions_parent_valid",
        ),
        sa.CheckConstraint("parent_session_id IS DISTINCT FROM id", name="task_sessions_no_self_parent"),
        sa.CheckConstraint(
            "kind<>'main' OR root_work_order_id=work_order_id", name="task_sessions_main_root_valid"
        ),
    )
    op.create_index(
        "task_sessions_one_owner_per_order",
        "task_sessions",
        ["project_id", "work_order_id"],
        unique=True,
        postgresql_where=sa.text("kind<>'consultation'"),
    )
    op.create_index(
        "task_sessions_parent_idx", "task_sessions", ["project_id", "parent_session_id", "created_at"]
    )
    op.create_table(
        "task_session_messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("source_session_id", UUID),
        sa.Column("source_turn_id", UUID),
        sa.Column("client_message_id", UUID),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="task_session_messages_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "session_id", "sequence", name="task_session_messages_sequence_unique"
        ),
        sa.UniqueConstraint("project_id", "client_message_id", name="task_session_messages_client_unique"),
        sa.UniqueConstraint(
            "project_id", "source_turn_id", "kind", name="task_session_messages_legacy_unique"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "source_session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_turn_id"],
            ["task_conversation_turns.project_id", "task_conversation_turns.id"],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_session_messages_actor_nonblank"),
        sa.CheckConstraint("sequence > 0", name="task_session_messages_sequence_positive"),
        sa.CheckConstraint(
            "kind IN ('operator','agent','system','legacy_operator','legacy_agent')",
            name="task_session_messages_kind_valid",
        ),
        sa.CheckConstraint("body ~ '[^[:space:]]'", name="task_session_messages_body_nonblank"),
        sa.CheckConstraint(
            "(kind IN ('legacy_operator','legacy_agent')) = (source_turn_id IS NOT NULL)",
            name="task_session_messages_legacy_source_valid",
        ),
    )
    op.create_index(
        "task_session_messages_timeline",
        "task_session_messages",
        ["project_id", "session_id", "sequence"],
    )
    op.execute(
        """CREATE FUNCTION guard_task_session_message() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'task session messages are append only';
        END; $$"""
    )
    op.execute(
        "CREATE TRIGGER task_session_messages_append_only BEFORE UPDATE OR DELETE "
        "ON task_session_messages FOR EACH ROW EXECUTE FUNCTION guard_task_session_message()"
    )
    op.create_table(
        "task_session_attempts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("source_message_id", UUID),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("provider", sa.Text()),
        sa.Column("model_id", sa.Text()),
        sa.Column("provider_thread_id", sa.Text()),
        sa.Column("provider_turn_id", sa.Text()),
        sa.Column("prompt_sha256", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(20, 10)),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "id", name="task_session_attempts_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "source_message_id", name="task_session_attempts_source_message_unique"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_message_id"],
            ["task_session_messages.project_id", "task_session_messages.id"],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_session_attempts_actor_nonblank"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','unknown')",
            name="task_session_attempts_status_valid",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="task_session_attempts_input_valid"
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="task_session_attempts_output_valid"
        ),
        sa.CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="task_session_attempts_cost_valid"),
        sa.CheckConstraint(
            "prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'",
            name="task_session_attempts_prompt_valid",
        ),
    )
    op.create_index(
        "task_session_attempts_one_active",
        "task_session_attempts",
        ["project_id", "session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index("task_session_attempts_pending", "task_session_attempts", ["status", "created_at", "id"])
    op.execute(
        """CREATE FUNCTION guard_task_session_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status NOT IN ('queued','running') THEN
            RAISE EXCEPTION 'completed task session attempts are immutable';
          END IF;
          IF ROW(NEW.project_id,NEW.actor_id,NEW.root_work_order_id,NEW.session_id,
                 NEW.source_message_id,NEW.created_at) IS DISTINCT FROM
             ROW(OLD.project_id,OLD.actor_id,OLD.root_work_order_id,OLD.session_id,
                 OLD.source_message_id,OLD.created_at) THEN
            RAISE EXCEPTION 'task session attempt binding is immutable';
          END IF;
          IF (OLD.status='queued' AND NEW.status NOT IN ('running','failed','unknown')) OR
             (OLD.status='running' AND NEW.status NOT IN ('done','failed','unknown')) THEN
            RAISE EXCEPTION 'task session attempt transition is invalid';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.execute(
        "CREATE TRIGGER task_session_attempt_finalize BEFORE UPDATE ON task_session_attempts "
        "FOR EACH ROW EXECUTE FUNCTION guard_task_session_attempt()"
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("task_session_attempts", "task_session_messages", "task_sessions"):
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):  # noqa: S608
            raise RuntimeError("team session data exists; downgrade would discard saved work")
    op.execute("DROP TRIGGER task_session_attempt_finalize ON task_session_attempts")
    op.execute("DROP FUNCTION guard_task_session_attempt()")
    op.drop_index("task_session_attempts_pending", table_name="task_session_attempts")
    op.drop_index("task_session_attempts_one_active", table_name="task_session_attempts")
    op.drop_table("task_session_attempts")
    op.execute("DROP TRIGGER task_session_messages_append_only ON task_session_messages")
    op.execute("DROP FUNCTION guard_task_session_message()")
    op.drop_index("task_session_messages_timeline", table_name="task_session_messages")
    op.drop_table("task_session_messages")
    op.drop_index("task_sessions_parent_idx", table_name="task_sessions")
    op.drop_index("task_sessions_one_owner_per_order", table_name="task_sessions")
    op.drop_table("task_sessions")
