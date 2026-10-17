"""Thin operator routes; credential validation never echoes submitted secret inputs."""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from onefoundry.workflow_api import execute
from onefoundry.zoho_mail_actions import propose_manual
from onefoundry.zoho_mail_contracts import (
    ZohoConnectionOut,
    ZohoCredentials,
    ZohoEnabled,
    ZohoEndpoint,
    ZohoManualProposal,
    ZohoRead,
    ZohoSetup,
    ZohoSignIn,
    ZohoSignInOut,
    ZohoWrite,
)
from onefoundry.zoho_mail_dispatch import list_actions
from onefoundry.zoho_mail_signin import ZohoSignInStore
from onefoundry.zoho_mail_store import ZohoMailStore


class _HideOAuthCallback(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's request target contains the one-use OAuth code. Drop this
        # access record rather than persisting a credential in its query string.
        target = record.getMessage()
        return not ("/mail/zoho/connections/" in target and "/oauth/callback" in target)


logging.getLogger("uvicorn.access").addFilter(_HideOAuthCallback())


def make_zoho_mail_router(store: ZohoMailStore) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/mail/zoho")

    def signin() -> ZohoSignInStore:
        return ZohoSignInStore(store)

    @router.get("/connections", response_model=list[ZohoConnectionOut])
    def list_connections(project_id: UUID) -> list[ZohoConnectionOut]:
        return execute(lambda: [ZohoConnectionOut.model_validate(row) for row in store.list(project_id)])

    @router.post("/connections", response_model=ZohoConnectionOut, status_code=201)
    def create(project_id: UUID, data: ZohoSetup) -> ZohoConnectionOut:
        return execute(lambda: ZohoConnectionOut.model_validate(store.create(project_id, data)))

    @router.post("/connections/{connection_id}/endpoint", response_model=ZohoConnectionOut)
    async def save_endpoint(project_id: UUID, connection_id: UUID, request: Request) -> ZohoConnectionOut:
        size = 0
        chunks: list[bytes] = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8192:
                raise HTTPException(413, "Zoho server request is too large")
            chunks.append(chunk)
        try:
            data = ZohoEndpoint.model_validate_json(b"".join(chunks))
        except ValidationError:
            raise HTTPException(422, "Invalid Zoho server settings; inputs are not echoed") from None
        return await run_in_threadpool(
            execute,
            lambda: ZohoConnectionOut.model_validate(signin().endpoint(project_id, connection_id, data)),
        )

    @router.post("/connections/{connection_id}/sign-in", response_model=ZohoSignInOut)
    def start_signin(project_id: UUID, connection_id: UUID, data: ZohoSignIn) -> ZohoSignInOut:
        return execute(
            lambda: ZohoSignInOut.model_validate(
                signin().start(project_id, connection_id, data.expected_version)
            )
        )

    @router.get("/connections/{connection_id}/oauth/authorize")
    def authorize(project_id: UUID, connection_id: UUID) -> RedirectResponse:
        uri = execute(lambda: signin().authorize(project_id, connection_id))
        return RedirectResponse(
            uri, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )

    @router.get("/connections/{connection_id}/oauth/callback")
    def callback(project_id: UUID, connection_id: UUID, request: Request) -> RedirectResponse:
        result = "failed"
        if len(request.scope.get("query_string", b"")) <= 8192:
            params = request.query_params
            try:
                saved = signin().callback(
                    project_id,
                    connection_id,
                    params.get("state", ""),
                    params.get("code"),
                    bool(params.get("error")),
                    params.get("iss"),
                )
                result = "ready" if saved["status"] == "ready" else "check"
            except Exception:
                # Never echo or log a one-use callback code, issuer response or
                # URL. Domain owners persist fixed failure codes when possible.
                result = "failed"
        uri = f"/#project/{project_id}/connections?zoho={result}"
        return RedirectResponse(
            uri, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )

    @router.post("/connections/{connection_id}/credentials", response_model=ZohoConnectionOut)
    async def credentials(project_id: UUID, connection_id: UUID, request: Request) -> ZohoConnectionOut:
        size = 0
        chunks: list[bytes] = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8192:
                raise HTTPException(413, "Zoho credential request is too large")
            chunks.append(chunk)
        try:
            data = ZohoCredentials.model_validate_json(b"".join(chunks))
        except ValidationError:
            raise HTTPException(
                422, "Invalid Zoho credentials or connection version; inputs are not echoed"
            ) from None
        return await run_in_threadpool(
            execute,
            lambda: ZohoConnectionOut.model_validate(store.credentials(project_id, connection_id, data)),
        )

    @router.post("/connections/{connection_id}/probe", response_model=ZohoConnectionOut)
    def probe(project_id: UUID, connection_id: UUID) -> ZohoConnectionOut:
        return execute(lambda: ZohoConnectionOut.model_validate(store.probe(project_id, connection_id)))

    @router.post("/connections/{connection_id}/enabled", response_model=ZohoConnectionOut)
    def enabled(project_id: UUID, connection_id: UUID, data: ZohoEnabled) -> ZohoConnectionOut:
        return execute(
            lambda: ZohoConnectionOut.model_validate(
                store.set_enabled(project_id, connection_id, data.enabled, data.expected_version)
            )
        )

    @router.post("/read")
    def read(project_id: UUID, data: ZohoRead) -> dict[str, object]:
        from onefoundry.zoho_mail_mcp import ZohoMcpError

        try:
            return execute(lambda: store.read(project_id, data))
        except ZohoMcpError as error:
            raise HTTPException(502, error.code) from None

    @router.post("/actions", status_code=201)
    def propose(project_id: UUID, data: ZohoManualProposal) -> dict[str, object]:
        fields = data.model_dump(exclude={"run_id"})
        return execute(
            lambda: propose_manual(store, project_id, data.run_id, ZohoWrite.model_validate(fields))
        )

    @router.get("/actions")
    def actions(project_id: UUID) -> list[dict[str, object]]:
        return execute(lambda: list_actions(store, project_id))

    return router
