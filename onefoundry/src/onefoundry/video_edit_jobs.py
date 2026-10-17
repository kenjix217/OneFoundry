"""Durable edit intent and lifecycle. Only the gateway publishes artifacts/events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_tool_admission import RunContext, ToolDenied
from onefoundry.video_edit_contract import EditRequest, canonical, digest

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def public_job(row: dict[str, Any]) -> dict[str, Any]:
    output = {
        "job_id": str(row["id"]),
        "status": row["status"],
        "attempt": row["attempt"],
        "recipe_sha256": row["recipe_sha256"],
    }
    if row.get("reason"):
        output["reason"] = row["reason"]
    if row["status"] == "done":
        output.update(row["receipt"])
        output.update({"artifact_id": str(row["artifact_id"]), "artifact_run_id": str(row["run_id"])})
    return output


class VideoEditJobs:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        settings = gateway.live_text_settings
        if settings is None or not settings.video_edit_enabled:
            raise ToolDenied("local video editing is disabled")
        self.settings = settings

    def get(self, project: UUID, job: UUID, connection: Connection | None = None) -> dict[str, Any]:
        if connection is None:
            with self.gateway.engine.connect() as owned:
                return self.get(project, job, owned)
        row = (
            connection.execute(
                text("SELECT * FROM video_edit_jobs WHERE project_id=:p AND id=:id"),
                {"p": project, "id": job},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("video edit job not found")
        return dict(row)

    def queue(self, connection: Connection, context: RunContext, recipe: EditRequest) -> dict[str, Any]:
        settings = self.settings
        if (
            len(recipe.scenes) > settings.video_edit_max_scenes
            or len(recipe.captions) > settings.video_edit_max_captions
            or recipe.duration > settings.media_max_duration_seconds
            or recipe.width > settings.media_max_width
            or recipe.height > settings.media_max_height
        ):
            raise ToolDenied("edit exceeds configured media bounds")
        encoded = recipe.model_dump(mode="json")
        sha = digest(encoded)
        prior = (
            connection.execute(
                text(
                    "SELECT * FROM video_edit_jobs WHERE project_id=:p AND work_order_id=:o AND "
                    "request_key=:key"
                ),
                {"p": context.project_id, "o": context.work_order_id, "key": recipe.request_key},
            )
            .mappings()
            .one_or_none()
        )
        if prior is not None:
            if prior["recipe_sha256"] != sha or prior["profile_id"] != context.profile_id:
                raise Conflict("edit request key is already bound to a different recipe or profile")
            return public_job(dict(prior))
        count = connection.scalar(
            text("SELECT count(*) FROM video_edit_jobs WHERE project_id=:p AND work_order_id=:o"),
            {"p": context.project_id, "o": context.work_order_id},
        )
        if count >= settings.video_edit_max_jobs_per_order:
            raise ToolDenied("work order media job quota reached")
        requested: dict[UUID, str] = {scene.asset_id: scene.kind for scene in recipe.scenes}
        for track in recipe.audio:
            if track.asset_id in requested:
                raise ToolDenied("one asset cannot be used as both picture and audio")
            requested[track.asset_id] = "audio"
        assets = []
        for asset_id, kind in requested.items():
            row = (
                connection.execute(
                    text(
                        "SELECT id,kind,mime_type,sha256,byte_size,provenance FROM video_edit_assets "
                        "WHERE project_id=:p AND id=:id AND (work_order_id IS NULL OR work_order_id=:o)"
                    ),
                    {"p": context.project_id, "id": asset_id, "o": context.work_order_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["kind"] != kind:
                raise ToolDenied("media asset is absent, out of scope or has a different kind")
            assets.append({**dict(row), "id": str(asset_id)})
        job = uuid4()
        connection.execute(
            text(
                "INSERT INTO video_edit_jobs (id,project_id,actor_id,work_order_id,run_id,profile_id,"
                "request_key,recipe,recipe_sha256,assets) VALUES "
                "(:id,:p,:actor,:o,:r,:profile,:key,CAST(:recipe AS jsonb),:sha,CAST(:assets AS jsonb))"
            ),
            {
                "id": job,
                "p": context.project_id,
                "actor": f"profile:{context.profile_id}",
                "o": context.work_order_id,
                "r": context.run_id,
                "profile": context.profile_id,
                "key": recipe.request_key,
                "recipe": canonical(encoded).decode(),
                "sha": sha,
                "assets": canonical(assets).decode(),
            },
        )
        self.gateway._event(
            connection,
            context.project_id,
            context.work_order_id,
            context.run_id,
            "video_edit_queued",
            {
                "job_id": str(job),
                "recipe_sha256": sha,
                "assets": [{"asset_id": a["id"], "sha256": a["sha256"]} for a in assets],
            },
            actor_id=f"profile:{context.profile_id}",
        )
        return public_job(self.get(context.project_id, job, connection))

    def cancel(self, connection: Connection, row: dict[str, Any]) -> dict[str, Any]:
        target = {"queued": "canceled", "running": "cancel_requested"}.get(row["status"])
        if target:
            connection.execute(
                text(
                    "UPDATE video_edit_jobs SET status=:status,reason='operator_or_agent_canceled',"
                    "finished_at=CASE WHEN :status='canceled' THEN clock_timestamp() ELSE finished_at END "
                    "WHERE project_id=:p AND id=:id AND status=:before"
                ),
                {"status": target, "p": row["project_id"], "id": row["id"], "before": row["status"]},
            )
            self.gateway._event(
                connection,
                row["project_id"],
                row["work_order_id"],
                row["run_id"],
                "video_edit_cancel_requested",
                {"job_id": str(row["id"]), "status": target},
            )
        return public_job(self.get(row["project_id"], row["id"], connection))

    def retry(self, project: UUID, job: UUID) -> dict[str, Any]:
        if self.gateway.actor_id != self.settings.operator_actor_id:
            raise ToolDenied("video edit retry requires the operator boundary")
        with self.gateway.engine.begin() as connection:
            connection.execute(text("SELECT 1 FROM projects WHERE id=:p FOR UPDATE"), {"p": project})
            row = self.get(project, job, connection)
            connection.execute(
                text("SELECT 1 FROM video_edit_jobs WHERE project_id=:p AND id=:id FOR UPDATE"),
                {"p": project, "id": job},
            )
            row = self.get(project, job, connection)
            if row["status"] != "failed" or row["attempt"] >= self.settings.video_edit_max_attempts:
                raise Conflict("only a known failed edit within its attempt bound may be retried")
            active = connection.scalar(
                text(
                    "SELECT 1 FROM projects p JOIN work_orders w ON w.project_id=p.id "
                    "WHERE p.id=:p AND p.status='active' AND w.id=:o AND w.status='running'"
                ),
                {"p": project, "o": row["work_order_id"]},
            )
            if active is None:
                raise Conflict("edit work order is not active")
            connection.execute(
                text(
                    "UPDATE video_edit_jobs SET status='queued',reason=NULL,finished_at=NULL "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"p": project, "id": job},
            )
            self.gateway._event(
                connection,
                project,
                row["work_order_id"],
                row["run_id"],
                "video_edit_retry_requested",
                {"job_id": str(job), "attempt": row["attempt"]},
            )
            return public_job(self.get(project, job, connection))
