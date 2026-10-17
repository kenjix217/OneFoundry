"""Recognize a durable operator recording request without inventing a result."""

from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection


def waiting_for_recording(
    connection: Connection, project_id: UUID, session_id: UUID | None = None, *, root_id: UUID | None = None
) -> bool:
    if (session_id is None) == (root_id is None):
        raise ValueError("exactly one recording-wait scope is required")
    return (
        connection.scalar(
            text(
                "SELECT 1 FROM events e JOIN task_session_attempts a "
                "ON a.project_id=e.project_id AND a.id=e.run_id "
                "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                "WHERE a.project_id=:p AND s.status<>'closed' "
                "AND (CAST(:s AS uuid) IS NULL OR a.session_id=:s) "
                "AND (CAST(:root AS uuid) IS NULL OR a.root_work_order_id=:root) "
                "AND e.event_type='media_capture_requested' "
                "AND NOT EXISTS(SELECT 1 FROM workspace_artifacts w WHERE w.project_id=e.project_id "
                "AND w.run_id=e.run_id AND w.call_id='media_'||replace(e.payload->>'capture_id','-','') "
                "AND w.status IN ('ready','unknown')) LIMIT 1"
            ),
            {"p": project_id, "s": session_id, "root": root_id},
        )
        is not None
    )


def recording_wait_reason(
    connection: Connection, project_id: UUID, session_id: UUID
) -> Literal["local_recording", "recording_ready", "recording_unconfirmed"] | None:
    status = connection.scalar(
        text(
            "SELECT COALESCE(w.status,'missing') FROM events e JOIN task_session_attempts a "
            "ON a.project_id=e.project_id AND a.id=e.run_id LEFT JOIN workspace_artifacts w "
            "ON w.project_id=e.project_id AND w.run_id=e.run_id "
            "AND w.call_id='media_'||replace(e.payload->>'capture_id','-','') "
            "WHERE a.project_id=:p AND a.session_id=:s AND e.event_type='media_capture_requested' "
            "ORDER BY CASE WHEN w.status='unknown' THEN 0 WHEN w.status='ready' THEN 2 ELSE 1 END, "
            "e.created_at DESC,e.id LIMIT 1"
        ),
        {"p": project_id, "s": session_id},
    )
    if status is None:
        return None
    if status == "ready":
        return "recording_ready"
    if status == "unknown":
        return "recording_unconfirmed"
    return "local_recording"
