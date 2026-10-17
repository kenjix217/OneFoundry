"""Separate trusted supervisor: claim, render, cancel and publish exact local artifacts."""

from __future__ import annotations

import argparse
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from onefoundry.artifact_workspace import ArtifactWorkspaceError
from onefoundry.input_file_bytes import InputFileBytes, InputFileInvalid
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from onefoundry.video_edit_contract import canonical
from onefoundry.video_edit_jobs import VideoEditJobs, public_job
from runner.video_edit_supervisor import VideoWorkerCanceled, VideoWorkerFailure, policy, run_render


class VideoEditWorker:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        self.jobs = VideoEditJobs(gateway)
        self.settings = self.jobs.settings

    def _permitted(self, row: dict[str, Any]) -> bool:
        with self.gateway.engine.connect() as connection:
            return (
                connection.scalar(
                    text(
                        "SELECT 1 FROM projects p JOIN work_orders w ON w.project_id=p.id "
                        "JOIN profiles pr ON pr.project_id=p.id AND pr.id=:profile "
                        "WHERE p.id=:p AND p.status='active' AND w.id=:o AND w.status='running' "
                        "AND pr.allowed_tools ? 'edit_video_v1'"
                    ),
                    {"p": row["project_id"], "o": row["work_order_id"], "profile": row["profile_id"]},
                )
                is not None
            )

    def _event(self, connection: Any, row: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
        self.gateway._event(
            connection,
            row["project_id"],
            row["work_order_id"],
            row["run_id"],
            kind,
            {"job_id": str(row["id"]), "attempt": row["attempt"], **payload},
            actor_id="runner:video-edit",
        )

    def _settle(self, row: dict[str, Any], status: str, reason: str) -> None:
        with self.gateway.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE video_edit_jobs SET status=:s,reason=:reason,finished_at=clock_timestamp() "
                    "WHERE project_id=:p AND id=:id AND attempt=:attempt "
                    "AND status IN ('running','cancel_requested')"
                ),
                {
                    "s": status,
                    "reason": reason,
                    "p": row["project_id"],
                    "id": row["id"],
                    "attempt": row["attempt"],
                },
            )
            if changed.rowcount == 1:
                self._event(connection, row, "video_edit_finished", {"status": status, "reason": reason})

    def reconcile_expired(self) -> int:
        with self.gateway.engine.begin() as connection:
            expired = (
                connection.execute(
                    text(
                        "SELECT * FROM video_edit_jobs WHERE status IN ('running','cancel_requested') "
                        "AND started_at < clock_timestamp()-make_interval(secs => :seconds) "
                        "FOR UPDATE SKIP LOCKED"
                    ),
                    {
                        "seconds": self.settings.video_edit_wall_seconds
                        + self.settings.runner_receipt_wait_seconds
                    },
                )
                .mappings()
                .all()
            )
            for raw in expired:
                row = dict(raw)
                connection.execute(
                    text(
                        "UPDATE video_edit_jobs SET status='unknown',reason='worker_receipt_missing',"
                        "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                    ),
                    {"p": row["project_id"], "id": row["id"]},
                )
                self._event(
                    connection,
                    row,
                    "video_edit_finished",
                    {"status": "unknown", "reason": "worker_receipt_missing"},
                )
            return len(expired)

    def process_one(self, project: UUID | None = None) -> dict[str, Any] | None:
        with self.gateway.engine.begin() as connection:
            raw = (
                connection.execute(
                    text(
                        "SELECT * FROM video_edit_jobs WHERE status='queued' "
                        "AND (CAST(:p AS uuid) IS NULL OR "
                        "project_id=:p) "
                        "ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ),
                    {"p": project},
                )
                .mappings()
                .one_or_none()
            )
            if raw is None:
                return None
            row = dict(raw)
            row["attempt"] += 1
            connection.execute(
                text(
                    "UPDATE video_edit_jobs SET "
                    "status='running',attempt=:attempt,started_at=clock_timestamp(),"
                    "finished_at=NULL WHERE project_id=:p AND id=:id"
                ),
                {"attempt": row["attempt"], "p": row["project_id"], "id": row["id"]},
            )
            self._event(
                connection,
                row,
                "video_edit_started",
                {"recipe_sha256": row["recipe_sha256"], "image_id": self.settings.video_edit_image},
            )
        try:
            if not self._permitted(row):
                raise VideoWorkerCanceled("video_edit_permission_revoked")
            store = InputFileBytes(self.settings.video_edit_asset_root)
            contents = [
                store.read(row["project_id"], UUID(a["id"]), a["sha256"], a["byte_size"])
                for a in row["assets"]
            ]
            manifest = {
                "recipe": row["recipe"],
                "recipe_sha256": row["recipe_sha256"],
                "assets": row["assets"],
                "policy": policy(self.settings),
            }

            def canceled() -> bool:
                current = self.jobs.get(row["project_id"], row["id"])
                return (
                    current["status"] != "running"
                    or current["attempt"] != row["attempt"]
                    or not self._permitted(row)
                )

            output, receipt = run_render(manifest, contents, self.settings, canceled)
            self._publish(row, output, receipt)
        except VideoWorkerCanceled as error:
            self._settle(row, "canceled", error.reason)
        except VideoWorkerFailure as error:
            self._settle(row, "failed", error.reason)
        except InputFileInvalid:
            self._settle(row, "failed", "source_integrity_failure")
        except Exception as error:
            reason = "storage_or_worker_outcome_uncertain_" + type(error).__name__
            self._settle(row, "unknown", reason)
        return self.jobs.get(row["project_id"], row["id"])

    def _publish(self, row: dict[str, Any], output: bytes, receipt: dict[str, Any]) -> None:
        # Publish bytes before an INSERT-only immutable workspace identity. Any
        # interrupted publication is unknown; retries never overwrite or attach orphans.
        artifact_id = uuid4()
        saved = self.gateway._artifact_workspace().write(
            row["project_id"],
            row["run_id"],
            artifact_id,
            kind="video",
            mime_type="video/webm",
            content=output,
            max_video_bytes=self.settings.media_max_bytes,
        )
        with self.gateway.engine.begin() as connection:
            # Use the same project/order/profile lock order as the Tool Gateway;
            # cancellation and publication then serialize without duplicate artifacts.
            permitted = connection.scalar(
                text(
                    "SELECT 1 FROM projects p JOIN work_orders w ON w.project_id=p.id "
                    "JOIN profiles pr ON pr.project_id=p.id AND pr.id=:profile "
                    "WHERE p.id=:p AND p.status='active' AND w.id=:o AND w.status='running' "
                    "AND pr.allowed_tools ? 'edit_video_v1' FOR UPDATE OF p,w,pr"
                ),
                {"p": row["project_id"], "o": row["work_order_id"], "profile": row["profile_id"]},
            )
            connection.execute(
                text("SELECT 1 FROM video_edit_jobs WHERE project_id=:p AND id=:id FOR UPDATE"),
                {"p": row["project_id"], "id": row["id"]},
            )
            current = self.jobs.get(row["project_id"], row["id"], connection)
            if current["status"] != "running" or current["attempt"] != row["attempt"] or permitted is None:
                raise VideoWorkerCanceled("video_edit_canceled_before_publication")
            if receipt["sha256"] != saved.sha256 or receipt["byte_size"] != saved.byte_size:
                raise ArtifactWorkspaceError("render receipt differs from stored bytes")
            connection.execute(
                text(
                    "INSERT INTO workspace_artifacts (id,project_id,run_id,actor_id,call_id,kind,mime_type,"
                    "relative_path,sha256,byte_size,status,finished_at) "
                    "VALUES "
                    "(:id,:p,:r,:actor,:call,'video','video/webm',:path,:sha,:size,'ready',clock_timestamp())"
                ),
                {
                    "id": artifact_id,
                    "p": row["project_id"],
                    "r": row["run_id"],
                    "actor": row["actor_id"],
                    "call": f"video-edit-{row['id']}",
                    "path": saved.relative_path,
                    "sha": saved.sha256,
                    "size": saved.byte_size,
                },
            )
            connection.execute(
                text(
                    "UPDATE video_edit_jobs SET "
                    "status='done',artifact_id=:artifact,receipt=CAST(:receipt AS jsonb),"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                ),
                {
                    "artifact": artifact_id,
                    "receipt": canonical(receipt).decode(),
                    "p": row["project_id"],
                    "id": row["id"],
                },
            )
            self._event(
                connection,
                row,
                "video_edit_finished",
                {
                    "status": "done",
                    "artifact_id": str(artifact_id),
                    "sha256": saved.sha256,
                    "byte_size": saved.byte_size,
                    "receipt": receipt,
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--project", type=UUID)
    arguments = parser.parse_args()
    settings = Settings()
    gateway = ToolGateway(
        create_engine(settings.database_url), settings.operator_actor_id, live_text_settings=settings
    )
    worker = VideoEditWorker(gateway)
    if arguments.once:
        result = worker.process_one(arguments.project)
        if result is not None:
            print(canonical(public_job(result)).decode())
        return
    while True:
        worker.reconcile_expired()
        if worker.process_one() is None:
            time.sleep(settings.onefoundry_coordinator_poll_seconds)


if __name__ == "__main__":
    main()
