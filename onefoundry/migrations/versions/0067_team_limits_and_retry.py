"""Record one bounded pre-send retry per team attempt and an earliest claim time."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067_team_limits_and_retry"
down_revision = "0066_team_provider_bindings"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("task_session_attempts", sa.Column("retry_of_attempt_id", UUID))
    op.add_column("task_session_attempts", sa.Column("not_before", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "task_session_attempts_retry_of_fk",
        "task_session_attempts",
        "task_session_attempts",
        ["project_id", "retry_of_attempt_id"],
        ["project_id", "id"],
    )
    op.create_index(
        "task_session_attempts_one_retry",
        "task_session_attempts",
        ["project_id", "retry_of_attempt_id"],
        unique=True,
        postgresql_where=sa.text("retry_of_attempt_id IS NOT NULL"),
    )
    op.execute(
        """CREATE FUNCTION guard_task_session_attempt_retry() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.retry_of_attempt_id,NEW.not_before) IS DISTINCT FROM
             ROW(OLD.retry_of_attempt_id,OLD.not_before) THEN
            RAISE EXCEPTION 'task session retry binding is immutable';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.execute(
        "CREATE TRIGGER task_session_attempt_retry_immutable BEFORE UPDATE ON task_session_attempts "
        "FOR EACH ROW EXECUTE FUNCTION guard_task_session_attempt_retry()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER task_session_attempt_retry_immutable ON task_session_attempts")
    op.execute("DROP FUNCTION guard_task_session_attempt_retry()")
    op.drop_index("task_session_attempts_one_retry", table_name="task_session_attempts")
    op.drop_constraint("task_session_attempts_retry_of_fk", "task_session_attempts", type_="foreignkey")
    op.drop_column("task_session_attempts", "not_before")
    op.drop_column("task_session_attempts", "retry_of_attempt_id")
