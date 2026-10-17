"""Operator setup and exact signed intake over the existing Tool Gateway."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from onefoundry.gateway_extension_webhooks import (
    PayloadRejected,
    SignatureRejected,
    WebhookCreate,
    WebhookCredentialPut,
    WebhookOut,
    WebhookPatch,
    create_webhook,
    list_webhooks,
    patch_webhook,
    receive_webhook,
    rotate_credential,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_extension_webhook_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/extensions/webhooks", response_model=list[WebhookOut])
    def list_subscriptions(project_id: UUID) -> list[WebhookOut]:
        return execute(lambda: list_webhooks(gateway, project_id))

    @router.post("/api/projects/{project_id}/extensions/webhooks", response_model=WebhookOut, status_code=201)
    def create_subscription(project_id: UUID, data: WebhookCreate) -> WebhookOut:
        return execute(lambda: create_webhook(gateway, project_id, data))

    @router.patch("/api/projects/{project_id}/extensions/webhooks/{webhook_id}", response_model=WebhookOut)
    def update_subscription(project_id: UUID, webhook_id: UUID, data: WebhookPatch) -> WebhookOut:
        return execute(lambda: patch_webhook(gateway, project_id, webhook_id, data))

    @router.put(
        "/api/projects/{project_id}/extensions/webhooks/{webhook_id}/credential",
        response_model=WebhookOut,
    )
    def update_credential(project_id: UUID, webhook_id: UUID, data: WebhookCredentialPut) -> WebhookOut:
        return execute(lambda: rotate_credential(gateway, project_id, webhook_id, data))

    @router.post("/api/extensions/webhooks/{webhook_id}", status_code=202)
    async def receive(webhook_id: UUID, request: Request) -> dict[str, object]:
        settings = gateway.live_text_settings
        if settings is None:
            raise HTTPException(503, "webhook settings are unavailable")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.extension_webhook_max_bytes:
                raise HTTPException(413, "webhook payload exceeds configured bound")
            chunks.append(chunk)
        try:
            return execute(
                lambda: receive_webhook(
                    gateway,
                    webhook_id,
                    timestamp=request.headers.get("x-onefoundry-timestamp"),
                    delivery_id=request.headers.get("x-onefoundry-delivery"),
                    signature=request.headers.get("x-onefoundry-signature"),
                    body=b"".join(chunks),
                )
            )
        except SignatureRejected as exc:
            raise HTTPException(401, str(exc)) from None
        except PayloadRejected as exc:
            raise HTTPException(400, str(exc)) from None

    return router
