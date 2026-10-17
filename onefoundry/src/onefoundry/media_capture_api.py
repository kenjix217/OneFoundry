"""Thin local recording upload and exact video playback-review endpoints."""

import asyncio
import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from onefoundry.media_capture_store import MediaCaptureStore, PlaybackVerification
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_media_capture_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")
    store = MediaCaptureStore(gateway)

    @router.get("/media-capture/limits")
    def limits(project_id: UUID) -> dict[str, Any]:
        # A project-scoped read also proves that the route cannot disclose
        # recording metadata from another project's owner.
        return execute(lambda: store.project_limits(project_id))

    @router.get("/work-orders/{order_id}/media-captures")
    def captures(project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
        return execute(lambda: store.list_for_order(project_id, order_id))

    @router.post("/runs/{run_id}/media-captures/{capture_id}")
    async def save_recording(
        project_id: UUID, run_id: UUID, capture_id: UUID, request: Request
    ) -> dict[str, Any]:
        execute(store.require_operator)
        if request.headers.get("content-type") != "video/webm":
            raise HTTPException(415, "recording upload must be video/webm")
        with gateway.engine.connect() as connection:
            execute(lambda: store.requested(connection, project_id, run_id, capture_id))
        maximum = int(store.limits()["max_bytes"])
        content = bytearray()
        settings = gateway.live_text_settings
        assert settings is not None
        try:
            async with asyncio.timeout(settings.media_max_duration_seconds):
                async for part in request.stream():
                    if len(content) + len(part) > maximum:
                        raise HTTPException(413, "recording exceeds the configured byte bound")
                    content.extend(part)
        except TimeoutError as exc:
            raise HTTPException(
                408, "recording upload exceeded its configured deadline; no save was started"
            ) from exc
        try:
            return await run_in_threadpool(
                execute,
                lambda: store.save(
                    project_id,
                    run_id,
                    capture_id,
                    bytes(content),
                    reviewed_window=request.headers.get("x-onefoundry-recording-reviewed") == "true",
                ),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/runs/{run_id}/workspace-artifacts/{artifact_id}/bytes")
    def video_bytes(project_id: UUID, run_id: UUID, artifact_id: UUID, request: Request) -> Response:
        content = execute(lambda: gateway.workspace_artifact_bytes(project_id, run_id, artifact_id))
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes"}
        requested = request.headers.get("range")
        if requested is None:
            return Response(content, media_type="video/webm", headers=headers)
        match = re.fullmatch(r"bytes=([0-9]{1,10})-([0-9]{0,10})", requested)
        if match is None:
            raise HTTPException(416, "a single bounded byte range is required")
        start = int(match[1])
        end = min(int(match[2]) if match[2] else len(content) - 1, len(content) - 1)
        if start > end or start >= len(content):
            raise HTTPException(416, "byte range is outside the saved video")
        headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
        return Response(content[start : end + 1], status_code=206, media_type="video/webm", headers=headers)

    @router.post("/runs/{run_id}/workspace-artifacts/{artifact_id}/verify-playback")
    def verify(
        project_id: UUID, run_id: UUID, artifact_id: UUID, data: PlaybackVerification
    ) -> dict[str, Any]:
        return execute(lambda: store.verify_playback(project_id, run_id, artifact_id, data))

    return router
