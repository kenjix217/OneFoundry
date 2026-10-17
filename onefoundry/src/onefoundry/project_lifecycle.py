"""Reversible project archive and Trash transitions through the Tool Gateway."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from onefoundry.gateway_errors import Conflict, NotFound

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class ProjectLifecycleStore:
    def __init__(self, gateway: "ToolGateway") -> None:
        self.gateway = gateway

    @staticmethod
    def _require_idle(connection: Connection, project_id: UUID) -> None:
        busy = connection.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM work_orders WHERE project_id=:id AND "
                "(status='running' OR (status='blocked' AND blocked_from_status='running'))) "
                "OR EXISTS(SELECT 1 FROM runs WHERE project_id=:id AND status='running') "
                "OR EXISTS(SELECT 1 FROM task_conversation_turns WHERE project_id=:id "
                "AND status='running') "
                "OR EXISTS(SELECT 1 FROM remote_channels WHERE project_id=:id AND enabled=true) "
                "OR EXISTS(SELECT 1 FROM remote_inbound WHERE project_id=:id AND status='processing') "
                "OR EXISTS(SELECT 1 FROM remote_outbound WHERE project_id=:id "
                "AND status IN ('approval_pending','pending','claimed')) "
                "OR EXISTS(SELECT 1 FROM runner_jobs WHERE project_id=:id AND status='queued') "
                "OR EXISTS(SELECT 1 FROM approval_requests WHERE project_id=:id AND status='pending')"
            ),
            {"id": project_id},
        )
        if busy:
            raise Conflict(
                "finish active runs, conversations, remote replies, runner jobs, and pending approvals "
                "before moving this project"
            )

    def transition(
        self,
        project_id: UUID,
        expected_version: int,
        action: str,
        confirm_name: str | None = None,
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            current = (
                connection.execute(
                    text(
                        "SELECT id,name,goal,desired_outcome,status,version,trash_from_status,created_at "
                        "FROM projects WHERE id=:id FOR UPDATE"
                    ),
                    {"id": project_id},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise NotFound("project not found")
            if current["version"] != expected_version:
                raise Conflict("project changed; refresh its saved state")

            prior_status = str(current["status"])
            target: str
            trash_from: str | None = None
            if action == "archive" and prior_status == "active":
                target = "archived"
                self._require_idle(connection, project_id)
            elif action == "unarchive" and prior_status == "archived":
                target = "active"
            elif action == "trash" and prior_status in {"active", "archived"}:
                if confirm_name != current["name"]:
                    raise Conflict("project name confirmation does not match")
                target = "trashed"
                trash_from = prior_status
                if prior_status == "active":
                    self._require_idle(connection, project_id)
            elif action == "restore" and prior_status == "trashed":
                target = str(current["trash_from_status"])
            else:
                raise Conflict("project state changed; refresh its saved state")

            updated = (
                connection.execute(
                    text(
                        "UPDATE projects SET status=:status,trash_from_status=:trash_from,"
                        "version=version+1 WHERE id=:id "
                        "RETURNING id,name,goal,desired_outcome,status,version,trash_from_status,created_at"
                    ),
                    {"id": project_id, "status": target, "trash_from": trash_from},
                )
                .mappings()
                .one()
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                f"project_{action}",
                {"from_status": prior_status, "to_status": target, "version": updated["version"]},
            )
            return dict(updated)


class ActiveProjectWriteMiddleware(BaseHTTPMiddleware):
    """Reject HTTP mutations in archived or trashed project workspaces."""

    def __init__(self, app: ASGIApp, engine: Engine) -> None:
        super().__init__(app)
        self.engine = engine

    def _status(self, project_id: UUID) -> str | None:
        with self.engine.connect() as connection:
            return cast(
                str | None,
                connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id}),
            )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return await call_next(request)
        parts = request.url.path.strip("/").split("/")
        if len(parts) < 4 or parts[:2] != ["api", "projects"]:
            return await call_next(request)
        try:
            project_id = UUID(parts[2])
        except ValueError:
            return await call_next(request)
        if len(parts) == 4 and parts[3] in {"archive", "unarchive", "trash", "restore"}:
            return await call_next(request)
        try:
            status = await run_in_threadpool(self._status, project_id)
        except SQLAlchemyError:
            return JSONResponse({"detail": "database unavailable"}, status_code=503)
        if status is None:
            return JSONResponse({"detail": "project not found"}, status_code=404)
        if status != "active":
            return JSONResponse(
                {"detail": "restore this project before changing its workspace"}, status_code=409
            )
        return await call_next(request)
