"""Operator HTTP routes for Microsoft and Google accounts."""

from uuid import UUID

from fastapi import APIRouter, Query, Response

from onefoundry.connector_schemas import (
    ConnectorCallback,
    ConnectorCreate,
    ConnectorOut,
    ConnectorStart,
    SignOutEverywhere,
    SignOutResult,
)
from onefoundry.connector_signout import sign_out
from onefoundry.connector_store import ConnectorStore
from onefoundry.workflow_api import execute


def make_connector_router(store: ConnectorStore) -> APIRouter:
    router = APIRouter()
    base = "/api/projects/{project_id}/connectors"

    @router.get(base, response_model=list[ConnectorOut])
    def list_accounts(project_id: UUID) -> list[ConnectorOut]:
        return execute(lambda: [ConnectorOut.model_validate(row) for row in store.list(project_id)])

    @router.post(base, response_model=ConnectorOut, status_code=201)
    def create(project_id: UUID, data: ConnectorCreate) -> ConnectorOut:
        return execute(lambda: ConnectorOut.model_validate(store.create(project_id, data)))

    @router.post(base + "/{account_id}/sign-in", response_model=ConnectorStart)
    def start(project_id: UUID, account_id: UUID) -> ConnectorStart:
        return execute(lambda: ConnectorStart.model_validate(store.start(project_id, account_id)))

    @router.post(base + "/{account_id}/poll", response_model=ConnectorOut)
    def poll(project_id: UUID, account_id: UUID) -> ConnectorOut:
        return execute(lambda: ConnectorOut.model_validate(store.poll(project_id, account_id)))

    @router.post(base + "/{account_id}/sign-out-everywhere", response_model=SignOutResult)
    def sign_out_everywhere(project_id: UUID, account_id: UUID, data: SignOutEverywhere) -> SignOutResult:
        return execute(lambda: SignOutResult.model_validate(sign_out(store, project_id, account_id, data)))

    @router.delete(base + "/{account_id}", status_code=204)
    def delete(project_id: UUID, account_id: UUID, expected_version: int = Query(ge=1)) -> Response:
        execute(lambda: store.disconnect(project_id, account_id, expected_version))
        return Response(status_code=204)

    @router.post("/api/connectors/google/callback", response_model=ConnectorOut)
    def callback(data: ConnectorCallback) -> ConnectorOut:
        return execute(lambda: ConnectorOut.model_validate(store.callback(data.state, data.code)))

    return router
