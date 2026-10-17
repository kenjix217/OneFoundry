"""Operator routes for SMTP setup and exact outgoing email proposals."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.smtp_accounts import SmtpAccounts
from onefoundry.smtp_outbound import list_emails, propose_manual
from onefoundry.smtp_schemas import (
    OutboundEmailOut,
    OutboundProposal,
    SmtpAccountCreate,
    SmtpAccountOut,
    SmtpEnabled,
)
from onefoundry.workflow_api import execute


def make_smtp_router(accounts: SmtpAccounts) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/mail/outgoing")

    @router.get("/accounts", response_model=list[SmtpAccountOut])
    def list_accounts(project_id: UUID) -> list[SmtpAccountOut]:
        return execute(lambda: [SmtpAccountOut.model_validate(row) for row in accounts.list(project_id)])

    @router.post("/accounts", response_model=SmtpAccountOut, status_code=201)
    def create_account(project_id: UUID, data: SmtpAccountCreate) -> SmtpAccountOut:
        return execute(lambda: SmtpAccountOut.model_validate(accounts.create(project_id, data)))

    @router.post("/accounts/{account_id}/probe", response_model=SmtpAccountOut)
    def probe(project_id: UUID, account_id: UUID) -> SmtpAccountOut:
        return execute(lambda: SmtpAccountOut.model_validate(accounts.probe(project_id, account_id)))

    @router.post("/accounts/{account_id}/enabled", response_model=SmtpAccountOut)
    def enable(project_id: UUID, account_id: UUID, data: SmtpEnabled) -> SmtpAccountOut:
        return execute(
            lambda: SmtpAccountOut.model_validate(
                accounts.set_enabled(project_id, account_id, data.enabled, data.expected_version)
            )
        )

    @router.get("/emails", response_model=list[OutboundEmailOut])
    def emails(project_id: UUID) -> list[OutboundEmailOut]:
        return execute(
            lambda: [
                OutboundEmailOut.model_validate(row) for row in list_emails(accounts.gateway, project_id)
            ]
        )

    @router.post("/emails", status_code=201)
    def propose(project_id: UUID, data: OutboundProposal) -> dict[str, str]:
        return execute(lambda: propose_manual(accounts.gateway, accounts, project_id, data))

    return router
