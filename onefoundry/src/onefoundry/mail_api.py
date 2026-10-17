"""Operator HTTP routes for read-only mailbox connections."""

from uuid import UUID

from fastapi import APIRouter, Query, Response

from onefoundry.mail_schemas import MailAccountCreate, MailAccountOut, MailApiAccountCreate, MailEnabled
from onefoundry.mail_store import MailStore
from onefoundry.workflow_api import execute


def make_mail_router(store: MailStore) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/mail/accounts")

    @router.get("", response_model=list[MailAccountOut])
    def list_accounts(project_id: UUID) -> list[MailAccountOut]:
        return execute(lambda: [MailAccountOut.model_validate(row) for row in store.list(project_id)])

    @router.post("", response_model=MailAccountOut, status_code=201)
    def create_account(project_id: UUID, data: MailAccountCreate) -> MailAccountOut:
        return execute(lambda: MailAccountOut.model_validate(store.create(project_id, data)))

    @router.post("/api", response_model=MailAccountOut, status_code=201)
    def create_api_account(project_id: UUID, data: MailApiAccountCreate) -> MailAccountOut:
        return execute(lambda: MailAccountOut.model_validate(store.create_api(project_id, data)))

    @router.post("/{account_id}/probe", response_model=MailAccountOut)
    def probe(project_id: UUID, account_id: UUID) -> MailAccountOut:
        return execute(lambda: MailAccountOut.model_validate(store.probe(project_id, account_id)))

    @router.post("/{account_id}/enabled", response_model=MailAccountOut)
    def set_enabled(project_id: UUID, account_id: UUID, data: MailEnabled) -> MailAccountOut:
        return execute(
            lambda: MailAccountOut.model_validate(
                store.set_enabled(project_id, account_id, data.enabled, data.expected_version)
            )
        )

    @router.delete("/{account_id}", status_code=204)
    def delete(project_id: UUID, account_id: UUID, expected_version: int = Query(ge=1)) -> Response:
        execute(lambda: store.delete(project_id, account_id, expected_version))
        return Response(status_code=204)

    return router
