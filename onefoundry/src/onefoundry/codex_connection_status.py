"""Read-only Codex login readiness shared by account readback and inference."""

import time
from typing import Any, Literal
from uuid import UUID

from onefoundry.codex_account import CodexConnectionError, decode_state
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError

CodexConnectionStatus = Literal[
    "connected", "expired", "reconnect_required", "pending", "login_expired", "login_incomplete"
]


def codex_state_status(state: dict[str, Any], ready: bool) -> CodexConnectionStatus:
    kind = state.get("kind")
    expiry = state.get("expires_at")
    if kind == "exchanged" or kind == "connected" and not ready:
        return "login_incomplete"
    if kind == "pending":
        return (
            "pending"
            if isinstance(expiry, int) and not isinstance(expiry, bool) and expiry > time.time()
            else "login_expired"
        )
    if kind != "connected" or state.get("reconnect_required"):
        return "reconnect_required"
    # Older saved credentials can omit expiry; retain their existing runtime contract.
    if expiry is not None:
        if not isinstance(expiry, int) or isinstance(expiry, bool):
            return "reconnect_required"
        if expiry <= time.time() + 60:
            return "expired"
    return "connected"


def codex_connection_status(
    secrets: ModelSecretStore, account_id: UUID, ready: bool
) -> CodexConnectionStatus:
    try:
        state = decode_state(secrets.read(account_id))
    except (SecretStoreError, CodexConnectionError):
        return "reconnect_required"
    return codex_state_status(state, ready)
