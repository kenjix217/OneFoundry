"""Operator media intake, durable job inspection and explicit retry/cancellation."""

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.input_file_bytes import InputFileInvalid, InputFileTooLarge
from onefoundry.tool_gateway import ToolGateway
from onefoundry.video_edit_assets import VideoEditAssets
from onefoundry.video_edit_jobs import VideoEditJobs, public_job
from onefoundry.workflow_api import execute


def make_video_edit_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/video-edits")

    @router.post("/assets", status_code=201)
    async def upload(
        project_id: UUID,
        request: Request,
        provenance: str = Header(alias="x-media-provenance", min_length=1, max_length=240),
        work_order_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            owner = execute(lambda: VideoEditAssets(gateway))
            async with asyncio.timeout(owner.settings.live_text_timeout_seconds):
                return await owner.upload(
                    project_id,
                    request.headers.get("content-type", ""),
                    provenance,
                    request.stream(),
                    work_order_id,
                )
        except TimeoutError:
            raise HTTPException(status_code=408, detail="media upload timed out") from None
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except Conflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Unavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        except InputFileTooLarge:
            raise HTTPException(status_code=413, detail="media upload exceeds byte bound") from None
        except InputFileInvalid:
            raise HTTPException(status_code=422, detail="media upload rejected") from None

    @router.get("/{job_id}")
    def status(project_id: UUID, job_id: UUID) -> dict[str, Any]:
        return execute(lambda: public_job(VideoEditJobs(gateway).get(project_id, job_id)))

    @router.post("/{job_id}/retry")
    def retry(project_id: UUID, job_id: UUID) -> dict[str, Any]:
        return execute(lambda: VideoEditJobs(gateway).retry(project_id, job_id))

    @router.post("/{job_id}/cancel")
    def cancel(project_id: UUID, job_id: UUID) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            jobs = VideoEditJobs(gateway)
            with gateway.engine.begin() as connection:
                connection.execute(text("SELECT 1 FROM projects WHERE id=:p FOR UPDATE"), {"p": project_id})
                return jobs.cancel(connection, jobs.get(project_id, job_id, connection))

        return execute(action)

    return router
