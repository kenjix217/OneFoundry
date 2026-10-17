"""Permit draft-only capture to take the existing project lock without UPDATE grants."""

from alembic import op

revision = "0073_memory_capture_lock"
down_revision = "0072_memory_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.memory_capture_lock_project(selected_project uuid)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $$
        DECLARE project_status text;
        BEGIN
            SELECT status INTO project_status FROM public.projects
                WHERE id=selected_project FOR UPDATE;
            RETURN project_status;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.memory_capture_lock_project(uuid) FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION public.memory_capture_lock_project(uuid)")
