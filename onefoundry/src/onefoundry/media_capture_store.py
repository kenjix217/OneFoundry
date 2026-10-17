"""Operator uploads and playback decisions for run-owned local video artifacts."""

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid5

from pydantic import Field
from sqlalchemy import text

from onefoundry.artifact_workspace import ArtifactInvalid, ArtifactWorkspaceError, _validate_content
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.role_video_capture import TOOL_ID
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class PlaybackVerification(StrictModel):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    watched_picture_and_captions: Literal[True]
    narration_reviewed_if_present: Literal[True]


class MediaCaptureStore:
    def __init__(self, gateway: "ToolGateway") -> None:
        self.gateway = gateway

    def limits(self) -> dict[str, Any]:
        settings = self.gateway.live_text_settings
        if settings is None:
            raise Conflict("media settings are unavailable")
        return {
            "enabled": settings.media_capture_enabled,
            "max_bytes": settings.media_max_bytes,
            "max_duration_seconds": settings.media_max_duration_seconds,
            "max_width": settings.media_max_width,
            "max_height": settings.media_max_height,
            "frame_rate": settings.media_frame_rate,
        }

    def project_limits(self, project_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            if connection.scalar(text("SELECT 1 FROM projects WHERE id=:p"), {"p": project_id}) is None:
                raise NotFound("project not found")
        return self.limits()

    def require_operator(self) -> None:
        settings = self.gateway.live_text_settings
        if settings is None or self.gateway.actor_id != settings.operator_actor_id:
            raise Conflict("only the operator can save or verify a recording")
        if not settings.media_capture_enabled:
            raise Conflict("operator-selected local recording is disabled")

    def requested(self, connection: Any, project_id: UUID, run_id: UUID, capture_id: UUID) -> dict[str, Any]:
        row = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND run_id=:r "
                "AND event_type='media_capture_requested' AND payload->>'capture_id'=:id"
            ),
            {"p": project_id, "r": run_id, "id": str(capture_id)},
        )
        if row is None:
            raise NotFound("recording request not found in this run")
        return dict(row)

    def list_for_order(self, project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            if (
                connection.scalar(
                    text("SELECT 1 FROM work_orders WHERE project_id=:p AND id=:o"),
                    {"p": project_id, "o": order_id},
                )
                is None
            ):
                raise NotFound("work order not found")
            rows: Sequence[dict[str, Any]] = (
                connection.execute(
                    text(
                        "SELECT e.payload FROM events e JOIN runs r ON r.project_id=e.project_id "
                        "AND r.id=e.run_id "
                        "JOIN task_session_attempts a ON a.project_id=r.project_id AND a.id=r.id "
                        "WHERE e.project_id=:p AND a.root_work_order_id=:o AND "
                        "e.event_type='media_capture_requested' "
                        "ORDER BY e.created_at,e.id LIMIT :limit"
                    ),
                    {
                        "p": project_id,
                        "o": order_id,
                        "limit": self.gateway.live_text_settings.team_max_assignments_per_order**2
                        + self.gateway.live_text_settings.team_max_assignments_per_order
                        if self.gateway.live_text_settings
                        else 0,
                    },
                )
                .scalars()
                .all()
            )
            result = []
            for payload in rows:
                item = dict(payload)
                artifact = (
                    connection.execute(
                        text(
                            "SELECT id,status,sha256 FROM workspace_artifacts WHERE project_id=:p AND "
                            "run_id=:r "
                            "AND call_id=:call"
                        ),
                        {
                            "p": project_id,
                            "r": UUID(item["run_id"]),
                            "call": "media_" + UUID(item["capture_id"]).hex,
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                if artifact is not None:
                    item.update(
                        {
                            "status": artifact["status"],
                            "artifact_id": str(artifact["id"]),
                            "sha256": artifact["sha256"],
                        }
                    )
                result.append(item)
            return result

    def save(
        self, project_id: UUID, run_id: UUID, capture_id: UUID, content: bytes, *, reviewed_window: bool
    ) -> dict[str, Any]:
        self.require_operator()
        settings = self.gateway.live_text_settings
        assert settings is not None
        if not reviewed_window:
            raise Conflict("review the selected window for sensitive content before saving")
        try:
            _validate_content(content, "video", settings.media_max_bytes)
        except ArtifactInvalid as exc:
            raise ValueError(str(exc)) from exc
        artifact_id = uuid5(capture_id, "saved-webm")
        digest = hashlib.sha256(content).hexdigest()
        work_order_id = self._claim_upload(project_id, run_id, capture_id, artifact_id, digest, len(content))
        if work_order_id is None:
            return self.gateway.workspace_artifact(project_id, run_id, artifact_id)
        outcome = self._store_upload(project_id, run_id, artifact_id, digest, content)
        outcome = self._finish_upload(
            project_id, run_id, capture_id, artifact_id, work_order_id, digest, outcome
        )
        if outcome != "ready":
            raise Conflict("recording storage outcome is unknown; inspect its saved intent and do not replay")
        return self.gateway.workspace_artifact(project_id, run_id, artifact_id)

    def _claim_upload(
        self, project_id: UUID, run_id: UUID, capture_id: UUID, artifact_id: UUID, digest: str, byte_size: int
    ) -> UUID | None:
        parameters = {"p": project_id, "r": run_id, "id": artifact_id}
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            request = self.requested(connection, project_id, run_id, capture_id)
            scope = (
                connection.execute(
                    text(
                        "SELECT r.work_order_id,p.allowed_tools,w.status,s.status AS session_status, "
                        "root.status AS root_status,proj.status AS project_status "
                        "FROM runs r JOIN profiles p ON p.project_id=r.project_id AND p.id=r.profile_id "
                        "JOIN work_orders w ON w.project_id=r.project_id AND w.id=r.work_order_id "
                        "JOIN task_session_attempts a ON a.project_id=r.project_id AND a.id=r.id "
                        "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                        "JOIN work_orders root ON root.project_id=s.project_id AND "
                        "root.id=s.root_work_order_id "
                        "JOIN projects proj ON proj.id=r.project_id "
                        "WHERE r.project_id=:p AND r.id=:r AND r.profile_id=:profile"
                    ),
                    {**parameters, "profile": UUID(request["profile_id"])},
                )
                .mappings()
                .one_or_none()
            )
            if scope is None or TOOL_ID not in scope["allowed_tools"]:
                raise Conflict("the requesting run's recording capability was revoked or is out of scope")
            prior = (
                connection.execute(
                    text(
                        "SELECT id,status,sha256,byte_size FROM workspace_artifacts WHERE "
                        "project_id=:p AND run_id=:r AND id=:id"
                    ),
                    parameters,
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None:
                if prior["status"] != "ready" or prior["sha256"] != digest or prior["byte_size"] != byte_size:
                    raise Conflict(
                        "recording already has a different or unconfirmed saved outcome; do not replay it"
                    )
                return None
            if (
                scope["status"] not in ("ready", "running")
                or scope["session_status"] == "closed"
                or scope["root_status"] not in ("ready", "running")
                or scope["project_status"] != "active"
            ):
                raise Conflict("recording task is closed; ask for a new revision")
            connection.execute(
                text(
                    "INSERT INTO workspace_artifacts (id,project_id,run_id,actor_id,call_id,kind,mime_type,"
                    "relative_path,sha256,byte_size,status) VALUES "
                    "(:id,:p,:r,:actor,:call,'video','video/webm',"
                    ":path,:sha,:size,'pending')"
                ),
                {
                    **parameters,
                    "actor": self.gateway.actor_id,
                    "call": "media_" + capture_id.hex,
                    "path": f"{project_id}/{run_id}/{artifact_id}.webm",
                    "sha": digest,
                    "size": byte_size,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                scope["work_order_id"],
                run_id,
                "media_save_started",
                {
                    "capture_id": str(capture_id),
                    "artifact_id": str(artifact_id),
                    "sha256": digest,
                    "reviewed_window": True,
                },
            )
        owned_order = scope["work_order_id"]
        assert isinstance(owned_order, UUID)
        return owned_order

    def _store_upload(
        self, project_id: UUID, run_id: UUID, artifact_id: UUID, digest: str, content: bytes
    ) -> Literal["ready", "unknown"]:
        settings = self.gateway.live_text_settings
        assert settings is not None
        try:
            self.gateway._artifact_workspace().write(
                project_id,
                run_id,
                artifact_id,
                kind="video",
                mime_type="video/webm",
                content=content,
                max_video_bytes=settings.media_max_bytes,
            )
            self.gateway._artifact_workspace().read(
                project_id,
                run_id,
                artifact_id,
                relative_path=f"{project_id}/{run_id}/{artifact_id}.webm",
                expected_sha256=digest,
                expected_byte_size=len(content),
                kind="video",
                mime_type="video/webm",
                max_video_bytes=settings.media_max_bytes,
            )
            return "ready"
        except (ArtifactWorkspaceError, OSError):
            return "unknown"

    def _finish_upload(
        self,
        project_id: UUID,
        run_id: UUID,
        capture_id: UUID,
        artifact_id: UUID,
        work_order_id: UUID,
        digest: str,
        outcome: Literal["ready", "unknown"],
    ) -> Literal["ready", "unknown"]:
        parameters = {"p": project_id, "r": run_id, "id": artifact_id}
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            still_allowed = connection.scalar(
                text(
                    "SELECT 1 FROM runs r JOIN profiles p ON p.project_id=r.project_id AND p.id=r.profile_id "
                    "JOIN work_orders w ON w.project_id=r.project_id AND w.id=r.work_order_id "
                    "JOIN task_session_attempts a ON a.project_id=r.project_id AND a.id=r.id "
                    "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                    "JOIN work_orders root ON root.project_id=s.project_id AND root.id=s.root_work_order_id "
                    "JOIN projects proj ON proj.id=r.project_id WHERE r.project_id=:p AND r.id=:r "
                    "AND jsonb_exists(p.allowed_tools,:tool) AND w.status IN ('ready','running') "
                    "AND s.status<>'closed' AND root.status IN ('ready','running') AND proj.status='active'"
                ),
                {**parameters, "tool": TOOL_ID},
            )
            if still_allowed is None:
                outcome = "unknown"
            connection.execute(
                text(
                    "UPDATE workspace_artifacts SET status=:status,finished_at=clock_timestamp() "
                    "WHERE project_id=:p AND run_id=:r AND id=:id AND status='pending'"
                ),
                {**parameters, "status": outcome},
            )
            self.gateway._event(
                connection,
                project_id,
                work_order_id,
                run_id,
                "media_save_finished",
                {
                    "capture_id": str(capture_id),
                    "artifact_id": str(artifact_id),
                    "sha256": digest,
                    "status": outcome,
                },
            )

        return outcome

    def verify_playback(
        self, project_id: UUID, run_id: UUID, artifact_id: UUID, data: PlaybackVerification
    ) -> dict[str, Any]:
        self.require_operator()
        saved = self.gateway.workspace_artifact(project_id, run_id, artifact_id)
        if saved["kind"] != "video" or saved["sha256"] != data.expected_sha256:
            raise Conflict("playback decision is not bound to the exact saved video")
        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            prior = connection.scalar(
                text(
                    "SELECT 1 FROM events WHERE project_id=:p AND run_id=:r AND "
                    "event_type='media_playback_verified' "
                    "AND payload->>'artifact_id'=:id AND payload->>'sha256'=:sha"
                ),
                {"p": project_id, "r": run_id, "id": str(artifact_id), "sha": data.expected_sha256},
            )
            if prior is None:
                work_order = connection.scalar(
                    text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:r"),
                    {"p": project_id, "r": run_id},
                )
                self.gateway._event(
                    connection,
                    project_id,
                    work_order,
                    run_id,
                    "media_playback_verified",
                    {
                        "artifact_id": str(artifact_id),
                        "sha256": data.expected_sha256,
                        "verification": "operator_watched_picture_captions_and_narration_if_present",
                    },
                )
        return {"artifact_id": str(artifact_id), "sha256": data.expected_sha256, "playback_verified": True}
