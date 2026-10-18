"""Resolve one project's configured text profile for a bounded model call."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.codex_account import CodexConnectionError, decode_state
from onefoundry.codex_connection_status import codex_state_status
from onefoundry.grok_account import GrokConnectionError
from onefoundry.grok_account import decode_state as decode_grok_state
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.provider_registry import provider_spec
from onefoundry.tool_gateway import Conflict


@dataclass(frozen=True)
class ProfileTextRuntime:
    profile_id: UUID
    instructions: str
    memory_scope: str
    account_id: UUID
    provider: str
    model_id: str
    credential_revision: int
    endpoint: str | None
    api_key: str | None
    codex_state: dict[str, Any] | None
    grok_state: dict[str, Any] | None


def resolve(
    connection: Any, project_id: UUID, profile_id: UUID, secrets: ModelSecretStore
) -> ProfileTextRuntime:
    profile = (
        connection.execute(
            text(
                "SELECT id,model,model_account_id,instructions,memory_scope "
                "FROM profiles WHERE project_id=:project_id AND id=:id FOR SHARE"
            ),
            {"project_id": project_id, "id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        profile is None
        or not profile["model"]
        or not profile["model_account_id"]
        or not str(profile["instructions"]).strip()
    ):
        raise Conflict("assigned agent needs instructions and a text model in Team")
    account = (
        connection.execute(
            text(
                "SELECT id,provider,auth_kind,endpoint,catalog_valid,credential_ready,"
                "credential_revision FROM model_accounts "
                "WHERE project_id=:project_id AND id=:id FOR SHARE"
            ),
            {"project_id": project_id, "id": profile["model_account_id"]},
        )
        .mappings()
        .one_or_none()
    )
    if (
        account is None
        or not (spec := provider_spec(str(account["provider"])))
        or not spec.text_runtime_available
    ):
        raise Conflict("assigned agent's text model account is unavailable")
    if not account["catalog_valid"]:
        raise Conflict("assigned agent's model catalog needs refresh")
    model_id = connection.scalar(
        text(
            "SELECT model_id FROM model_catalog WHERE project_id=:project_id "
            "AND account_id=:account_id AND model_id=:model_id AND category='text'"
        ),
        {"project_id": project_id, "account_id": account["id"], "model_id": profile["model"]},
    )
    if model_id is None:
        raise Conflict("assigned agent's model needs refresh")
    api_key: str | None = None
    codex_state: dict[str, Any] | None = None
    grok_state: dict[str, Any] | None = None
    if account["auth_kind"] == "api_key":
        if not account["credential_ready"]:
            raise Conflict("assigned agent's model credential is unavailable")
        try:
            api_key = secrets.read(account["id"])
        except SecretStoreError:
            raise Conflict("assigned agent's model credential is unavailable") from None
    elif account["auth_kind"] == "account_runtime" and account["provider"] == "openai_codex_runtime":
        if not account["credential_ready"]:
            raise Conflict("Codex model account is not connected")
        try:
            codex_state = decode_state(secrets.read(account["id"]))
        except (SecretStoreError, CodexConnectionError):
            raise Conflict("Codex model credential is unavailable") from None
        status = codex_state_status(codex_state, True)
        if status != "connected":
            raise Conflict("Codex model login expired or unavailable; reconnect account in AI connections")
    elif account["auth_kind"] == "account_runtime" and account["provider"] == "xai":
        if not account["credential_ready"]:
            raise Conflict("Grok model account is not connected")
        try:
            grok_state = decode_grok_state(secrets.read(account["id"]))
        except (SecretStoreError, GrokConnectionError):
            raise Conflict("Grok model credential is unavailable") from None
        if grok_state.get("kind") != "connected" or grok_state.get("expires_at", 0) <= time.time() + 60:
            raise Conflict("Grok model credential expired; refresh models")
    elif account["auth_kind"] != "none" or account["provider"] != "ollama":
        raise Conflict("assigned agent's model authentication is unavailable")
    return ProfileTextRuntime(
        profile_id=profile["id"],
        instructions=str(profile["instructions"]),
        memory_scope=str(profile["memory_scope"]),
        account_id=account["id"],
        provider=str(account["provider"]),
        model_id=str(profile["model"]),
        credential_revision=int(account["credential_revision"]),
        endpoint=account["endpoint"],
        api_key=api_key,
        codex_state=codex_state,
        grok_state=grok_state,
    )
