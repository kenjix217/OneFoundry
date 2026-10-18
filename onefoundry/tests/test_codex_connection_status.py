"""Account readback must describe usable Codex credentials without exposing them."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from onefoundry.codex_account import encode_state
from onefoundry.model_gateway import ModelGateway
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings


@pytest.mark.parametrize(
    "state,ready,expected,present",
    [
        ({"kind": "connected", "expires_at": 2000}, True, "connected", True),
        ({"kind": "connected"}, True, "connected", True),
        ({"kind": "connected", "expires_at": 1000}, True, "expired", True),
        ({"kind": "connected", "expires_at": 1060}, True, "expired", True),
        ({"kind": "connected", "expires_at": "bad"}, True, "reconnect_required", True),
        ({"kind": "connected", "expires_at": True}, True, "reconnect_required", True),
        ({"kind": "connected", "reconnect_required": True}, True, "reconnect_required", True),
        ({"kind": "pending", "expires_at": 2000}, False, "pending", False),
        ({"kind": "pending", "expires_at": 1000}, False, "login_expired", False),
        ({"kind": "exchanged", "tokens": {}}, False, "login_incomplete", False),
    ],
)
def test_codex_status_uses_saved_state_and_clock(
    tmp_path: Path, state: dict[str, Any], ready: bool, expected: str, present: bool
) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    store = ModelSecretStore(str(root))
    account_id = uuid4()
    store.write(account_id, encode_state({**state, "access_token": "private-access"}))
    gateway = ModelGateway(cast(Engine, None), "operator", store, Settings(db_password="test"))
    row = {
        "id": account_id,
        "provider": "openai_codex_runtime",
        "auth_kind": "account_runtime",
        "catalog_valid": True,
        "credential_ready": ready,
        "credential_revision": 1,
    }
    with patch("onefoundry.codex_connection_status.time.time", return_value=1000):
        result = gateway._redact(row)
    assert result["connection_status"] == expected
    assert result["credential_present"] is present
    assert result["catalog_valid"] is (expected == "connected")
    assert "private-access" not in str(result)


@pytest.mark.parametrize("contents", [None, "malformed"])
def test_missing_or_corrupt_codex_credentials_require_reconnect(tmp_path: Path, contents: str | None) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    store = ModelSecretStore(str(root))
    account_id = uuid4()
    if contents:
        store.write(account_id, contents)
    gateway = ModelGateway(cast(Engine, None), "operator", store, Settings(db_password="test"))
    result = gateway._redact(
        {
            "id": account_id,
            "provider": "openai_codex_runtime",
            "auth_kind": "account_runtime",
            "catalog_valid": True,
            "credential_ready": True,
            "credential_revision": 1,
        }
    )
    assert result["connection_status"] == "reconnect_required"
    assert not result["catalog_valid"]
