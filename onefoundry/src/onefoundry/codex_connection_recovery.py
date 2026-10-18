"""Explicit same-account Codex reconnection, preserving profile bindings."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.codex_account import (
    CodexConnectionError,
    CodexDeviceStartError,
    decode_state,
    encode_state,
    start_device_login,
)
from onefoundry.inference import TransportError
from onefoundry.model_providers import DiscoveryError
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError

if TYPE_CHECKING:
    from onefoundry.model_gateway import ModelGateway


def _preserve_exchange(secrets: ModelSecretStore, account_id: UUID) -> None:
    try:
        state = decode_state(secrets.read(account_id))
    except (SecretStoreError, CodexConnectionError):
        return
    if state["kind"] == "exchanged":
        raise CodexConnectionError("Codex tokens were received; complete the saved connection")


def reconnect(gateway: "ModelGateway", project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
    with gateway.engine.connect() as connection:
        revision = connection.scalar(
            text(
                "SELECT credential_revision FROM model_accounts WHERE project_id=:p AND id=:a "
                "AND provider='openai_codex_runtime' AND auth_kind='account_runtime'"
            ),
            {"p": project_id, "a": account_id},
        )
    if revision is None:
        return None
    _preserve_exchange(gateway.secrets, account_id)
    # A provider outage must leave the existing sign-in untouched.
    challenge = start_device_login(gateway.settings)
    with gateway.engine.begin() as connection:
        current = connection.scalar(
            text(
                "SELECT credential_revision FROM model_accounts WHERE project_id=:p AND id=:a "
                "AND provider='openai_codex_runtime' AND auth_kind='account_runtime' FOR UPDATE"
            ),
            {"p": project_id, "a": account_id},
        )
        if current != revision:
            raise CodexConnectionError("Codex connection changed; refresh status before reconnecting")
        _preserve_exchange(gateway.secrets, account_id)
        gateway.secrets.write(
            account_id,
            encode_state(
                {
                    "kind": "pending",
                    "device_code": challenge.device_code,
                    "user_code": challenge.user_code,
                    "expires_at": challenge.expires_at,
                    "interval_seconds": challenge.interval_seconds,
                }
            ),
        )
        connection.execute(
            text(
                "UPDATE model_accounts SET credential_ready=false,catalog_valid=false,"
                "credential_revision=credential_revision+1 WHERE project_id=:p AND id=:a"
            ),
            {"p": project_id, "a": account_id},
        )
    return gateway.codex_challenge(project_id, account_id)


def make_codex_recovery_router(gateway: "ModelGateway") -> APIRouter:
    from onefoundry.model_gateway import CodexStartOut

    router = APIRouter()

    @router.post("/accounts/{account_id}/codex/reconnect", response_model=CodexStartOut)
    def reconnect_account(project_id: UUID, account_id: UUID) -> CodexStartOut:
        try:
            result = reconnect(gateway, project_id, account_id)
        except CodexDeviceStartError as error:
            raise HTTPException(
                502, f"OpenAI sign-in endpoint returned HTTP {error.status}; login unchanged"
            ) from None
        except CodexConnectionError as error:
            raise HTTPException(409, str(error)) from None
        except (DiscoveryError, TransportError):
            raise HTTPException(502, "Codex device login unavailable; login unchanged") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(
                503, "model account store unavailable; refresh status before retrying"
            ) from None
        if result is None:
            raise HTTPException(404, "Codex model account not found in this project")
        return CodexStartOut.model_validate(result)

    return router
