"""Project-owned account metadata, redacted HTTP contracts, and catalog refresh."""

import time
from datetime import datetime
from math import ceil, isfinite
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import Engine, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from onefoundry.codex_account import (
    VERIFY_URL,
    CodexConnectionError,
    CodexConnectionPending,
    CodexDeviceStartError,
    CodexModelDiscoveryError,
    CodexTokenExchangeError,
    CodexTokenExchangeUnknown,
    decode_state,
    discover_codex_models,
    encode_state,
    finish_device_login,
    refresh_credential,
    start_device_login,
)
from onefoundry.codex_connection_recovery import make_codex_recovery_router
from onefoundry.codex_connection_status import CodexConnectionStatus, codex_connection_status
from onefoundry.grok_account import (
    DeviceChallenge as GrokDeviceChallenge,
)
from onefoundry.grok_account import (
    GrokConnectionDenied,
    GrokConnectionError,
    GrokConnectionExpired,
    GrokConnectionPending,
    GrokConnectionUnknown,
    GrokCredentialTooLarge,
    discover_grok_models,
    valid_account_token,
)
from onefoundry.grok_account import (
    decode_state as decode_grok_state,
)
from onefoundry.grok_account import (
    encode_state as encode_grok_state,
)
from onefoundry.grok_account import (
    finish_device_login as finish_grok_device_login,
)
from onefoundry.grok_account import (
    refresh_credential as refresh_grok_credential,
)
from onefoundry.grok_account import (
    start_device_login as start_grok_device_login,
)
from onefoundry.inference import TransportError
from onefoundry.model_providers import DiscoveryError, discover_models
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.provider_registry import provider_directory, provider_spec
from onefoundry.settings import Settings

Provider = str


def _new_grok_pending(challenge: GrokDeviceChallenge) -> dict[str, Any]:
    return {
        "kind": "pending",
        "device_code": challenge.device_code,
        "user_code": challenge.user_code,
        "verification_url": challenge.verification_url,
        "expires_at": challenge.expires_at,
        "interval_seconds": challenge.interval_seconds,
        "next_poll_at": time.time() + challenge.interval_seconds,
    }


def _grok_pending_state(state: dict[str, Any]) -> tuple[int, int, float]:
    """Validate the saved device challenge before polling or showing it."""
    expiry = state.get("expires_at")
    interval = state.get("interval_seconds")
    next_poll = state.get("next_poll_at", 0)
    device_code = state.get("device_code")
    user_code = state.get("user_code")
    url = state.get("verification_url")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        port = parsed.port if parsed is not None else None
    except ValueError:
        parsed = None
        port = None
    try:
        next_poll_value = float(next_poll)
    except (TypeError, ValueError, OverflowError):
        next_poll_value = float("nan")
    if (
        state.get("kind") != "pending"
        or not isinstance(expiry, int)
        or isinstance(expiry, bool)
        or expiry <= 0
        or not isinstance(interval, int)
        or isinstance(interval, bool)
        or not 1 <= interval <= 86_405
        or not isinstance(next_poll, (int, float))
        or isinstance(next_poll, bool)
        or not isfinite(next_poll_value)
        or next_poll_value < 0
        or not isinstance(device_code, str)
        or not 1 <= len(device_code) <= 4096
        or not isinstance(user_code, str)
        or not 1 <= len(user_code) <= 128
        or parsed is None
        or not isinstance(url, str)
        or len(url) > 2048
        or parsed.scheme != "https"
        or not (parsed.hostname == "x.ai" or (parsed.hostname or "").endswith(".x.ai"))
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise GrokConnectionError("Grok saved device challenge is invalid; start fresh sign-in")
    return expiry, interval, next_poll_value


def _grok_connected_expiry(state: dict[str, Any]) -> int:
    access = state.get("access_token")
    refresh = state.get("refresh_token")
    expiry = state.get("expires_at")
    if (
        state.get("kind") != "connected"
        or not valid_account_token(access)
        or not valid_account_token(refresh)
        or not isinstance(expiry, int)
        or isinstance(expiry, bool)
        or expiry <= 0
    ):
        raise GrokConnectionError("Grok account credential is invalid; sign in again")
    return expiry


def _grok_has_connected_secret(
    secrets: ModelSecretStore, account_id: UUID, revision: int | None = None
) -> bool:
    """A ready database flag only exposes a Grok account with usable saved tokens."""
    try:
        state = decode_grok_state(secrets.read(account_id))
        _grok_connected_expiry(state)
    except (SecretStoreError, GrokConnectionError):
        return False
    saved_revision = state.get("credential_revision")
    if revision is not None and saved_revision is not None:
        return type(saved_revision) is int and saved_revision == revision
    return True


def validate_local_endpoint(value: str) -> str:
    """Keep local model discovery on an operator owned loopback or Compose service."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "ollama"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or value != f"http://{parsed.netloc}{parsed.path}"
    ):
        raise ValueError("Ollama endpoint must be a local HTTP origin")
    return f"http://{parsed.hostname}:{parsed.port}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreate(StrictModel):
    provider: Provider
    label: str = Field(min_length=1, max_length=120)
    api_key: SecretStr | None = None
    endpoint: str | None = None

    @field_validator("provider")
    @classmethod
    def known_connectable_provider(cls, value: str) -> str:
        spec = provider_spec(value)
        if spec is None or not spec.connectable:
            raise ValueError("provider has no OneFoundry connection")
        return value


class CredentialPut(StrictModel):
    api_key: SecretStr


def require_valid_key(value: SecretStr) -> str:
    key = value.get_secret_value()
    if not key or len(key) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise HTTPException(422, "invalid model API credential")
    return key


class AccountOut(StrictModel):
    id: UUID
    project_id: UUID
    provider: Provider
    label: str
    auth_kind: Literal["api_key", "none", "account_runtime"]
    endpoint: str | None
    credential_present: bool
    catalog_valid: bool
    model_count: int
    last_refreshed_at: datetime | None
    connection_status: CodexConnectionStatus | None = None


class CatalogModelOut(StrictModel):
    id: UUID
    account_id: UUID
    provider: Provider
    model_id: str
    display_name: str
    category: Literal["text", "image", "audio", "embedding", "realtime", "other"]
    reasoning_efforts: list[str]
    available: bool


class RefreshOut(StrictModel):
    account: AccountOut
    models: list[CatalogModelOut]


class ProviderOut(StrictModel):
    id: Provider
    name: str
    auth_kind: Literal["api_key", "none", "account_runtime"]
    catalog_supported: bool
    text_runtime_available: bool
    connectable: bool


class CredentialChanged(Exception):
    pass


class CodexStartOut(StrictModel):
    account: AccountOut
    verification_url: str
    user_code: str
    expires_at: int
    interval_seconds: int


class CodexStart(StrictModel):
    label: str = Field(min_length=1, max_length=120)


class GrokStart(CodexStart):
    pass


class ModelGateway:
    def __init__(self, engine: Engine, actor_id: str, secrets: ModelSecretStore, settings: Settings) -> None:
        self.engine = engine
        self.actor_id = actor_id
        self.secrets = secrets
        self.settings = settings

    def reconcile_orphan_secrets(self) -> None:
        with self.engine.connect() as connection:
            stored_ids = cast(
                list[UUID], connection.execute(text("SELECT id FROM model_accounts")).scalars().all()
            )
            active_ids = set(stored_ids)
        self.secrets.remove_orphans(active_ids)

    def project_exists(self, project_id: UUID) -> bool:
        with self.engine.connect() as connection:
            return bool(connection.scalar(text("SELECT 1 FROM projects WHERE id=:id"), {"id": project_id}))

    def _account(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT a.id, a.project_id, a.provider, a.label, a.auth_kind, "
                        "a.endpoint, a.catalog_valid, "
                        "a.credential_revision, a.credential_ready, a.last_refreshed_at, "
                        "(SELECT count(*) FROM model_catalog c WHERE c.project_id=a.project_id "
                        "AND c.account_id=a.id) AS model_count "
                        "FROM model_accounts a WHERE a.project_id=:project_id AND a.id=:id"
                    ),
                    {"project_id": project_id, "id": account_id},
                )
                .mappings()
                .one_or_none()
            )
        return self._redact(dict(row)) if row else None

    def _redact(self, row: dict[str, Any]) -> dict[str, Any]:
        revision = int(row.pop("credential_revision"))
        ready = bool(row.pop("credential_ready"))
        if row["provider"] == "xai" and row["auth_kind"] == "account_runtime":
            row["credential_present"] = ready and _grok_has_connected_secret(
                self.secrets, row["id"], revision
            )
        else:
            row["credential_present"] = bool(
                ready and row["auth_kind"] != "none" and self.secrets.present(row["id"])
            )
        row["catalog_valid"] = bool(
            row["catalog_valid"] and (row["credential_present"] or row["auth_kind"] == "none")
        )
        if row["provider"] == "openai_codex_runtime" and row["auth_kind"] == "account_runtime":
            row["connection_status"] = codex_connection_status(self.secrets, row["id"], ready)
            row["catalog_valid"] = bool(row["catalog_valid"] and row["connection_status"] == "connected")
        return row

    def list_accounts(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT a.id, a.project_id, a.provider, a.label, a.auth_kind, "
                        "a.endpoint, a.catalog_valid, "
                        "a.credential_revision, a.credential_ready, a.last_refreshed_at, "
                        "(SELECT count(*) FROM model_catalog c WHERE c.project_id=a.project_id "
                        "AND c.account_id=a.id) AS model_count "
                        "FROM model_accounts a WHERE a.project_id=:project_id "
                        "ORDER BY a.provider,a.label,a.id"
                    ),
                    {"project_id": project_id},
                )
                .mappings()
                .all()
            )
        return [self._redact(dict(row)) for row in rows]

    def create(self, project_id: UUID, data: AccountCreate) -> dict[str, Any]:
        if data.provider in {"openai_codex_runtime", "xai"}:
            raise ValueError("account runtime requires device login")
        account_id = uuid4()
        local = data.provider == "ollama"
        endpoint = validate_local_endpoint(data.endpoint) if local and data.endpoint else None
        if local and endpoint is None or not local and (data.api_key is None or data.endpoint is not None):
            raise ValueError("invalid provider account transport")
        if data.api_key is not None:
            self.secrets.write(account_id, data.api_key.get_secret_value())
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO model_accounts "
                        "(id,project_id,actor_id,provider,label,auth_kind,endpoint,credential_ready) "
                        "VALUES (:id,:project_id,:actor_id,:provider,:label,:auth_kind,:endpoint,"
                        ":credential_ready)"
                    ),
                    {
                        "id": account_id,
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "provider": data.provider,
                        "label": data.label.strip(),
                        "auth_kind": "none" if local else "api_key",
                        "endpoint": endpoint,
                        "credential_ready": not local,
                    },
                )
        except Exception:
            self.secrets.remove(account_id)
            raise
        account = self._account(project_id, account_id)
        assert account is not None
        return account

    def start_codex(self, project_id: UUID, label: str) -> dict[str, Any]:
        if not label.strip():
            raise ValueError("model account label must not be blank")
        challenge = start_device_login(self.settings)
        account_id = uuid4()
        self.secrets.write(
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
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO model_accounts "
                        "(id,project_id,actor_id,provider,label,auth_kind,endpoint,credential_ready) "
                        "VALUES (:id,:project_id,:actor_id,'openai_codex_runtime',:label,"
                        "'account_runtime',NULL,false)"
                    ),
                    {
                        "id": account_id,
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "label": label.strip(),
                    },
                )
        except Exception:
            self.secrets.remove(account_id)
            raise
        account = self._account(project_id, account_id)
        assert account is not None
        return {
            "account": account,
            "verification_url": challenge.verification_url,
            "user_code": challenge.user_code,
            "expires_at": challenge.expires_at,
            "interval_seconds": challenge.interval_seconds,
        }

    def start_grok(self, project_id: UUID, label: str) -> dict[str, Any]:
        if not label.strip():
            raise ValueError("model account label must not be blank")
        challenge = start_grok_device_login(self.settings)
        account_id = uuid4()
        self.secrets.write(
            account_id,
            encode_grok_state(_new_grok_pending(challenge)),
        )
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO model_accounts "
                        "(id,project_id,actor_id,provider,label,auth_kind,endpoint,credential_ready) "
                        "VALUES (:id,:project_id,:actor_id,'xai',:label,'account_runtime',NULL,false)"
                    ),
                    {
                        "id": account_id,
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "label": label.strip(),
                    },
                )
        except Exception:
            self.secrets.remove(account_id)
            raise
        account = self._account(project_id, account_id)
        assert account is not None
        return {
            "account": account,
            "verification_url": challenge.verification_url,
            "user_code": challenge.user_code,
            "expires_at": challenge.expires_at,
            "interval_seconds": challenge.interval_seconds,
        }

    def finish_codex(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if account["auth_kind"] != "account_runtime" or account["provider"] != "openai_codex_runtime":
            raise ValueError("account is not a Codex connection")
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT credential_ready FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='openai_codex_runtime' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None:
                raise CredentialChanged()
            if not row[0]:
                state = decode_state(self.secrets.read(account_id))
                connected = (
                    state
                    if state["kind"] == "connected"
                    else finish_device_login(
                        state,
                        self.settings,
                        on_exchange=lambda exchanged: self.secrets.write(account_id, encode_state(exchanged)),
                    )
                )
                self.secrets.write(account_id, encode_state(connected))
                connection.execute(
                    text(
                        "UPDATE model_accounts SET credential_ready=true,"
                        "credential_revision=credential_revision+1 "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": account_id},
                )
        result = self._account(project_id, account_id)
        assert result is not None
        return result

    def restart_codex(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if account["provider"] != "openai_codex_runtime" or account["credential_present"]:
            raise ValueError("account has no pending Codex sign-in")
        if decode_state(self.secrets.read(account_id))["kind"] == "exchanged":
            raise CodexConnectionError("Codex tokens were received; complete the saved connection")
        challenge = start_device_login(self.settings)
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT credential_ready FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='openai_codex_runtime' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None or row[0]:
                raise CredentialChanged()
            if decode_state(self.secrets.read(account_id))["kind"] == "exchanged":
                raise CodexConnectionError("Codex tokens were received; complete the saved connection")
            self.secrets.write(
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
        return self.codex_challenge(project_id, account_id)

    def finish_grok(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if account["provider"] != "xai" or account["auth_kind"] != "account_runtime":
            raise ValueError("account is not a Grok subscription connection")
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT credential_ready,catalog_valid,credential_revision FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='xai' "
                    "AND auth_kind='account_runtime' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None:
                raise CredentialChanged()
            state = decode_grok_state(self.secrets.read(account_id))
            if row[0] and state["kind"] == "connected":
                _grok_connected_expiry(state)
                tagged_revision = state.get("credential_revision")
                if tagged_revision is not None and tagged_revision != row[2]:
                    # A token file can survive a failed finish transaction even
                    # when an older ready row and catalog also survived.
                    connection.execute(
                        text(
                            "UPDATE model_accounts SET catalog_valid=false,"
                            "credential_revision=credential_revision+1 "
                            "WHERE project_id=:project_id AND id=:id"
                        ),
                        {"project_id": project_id, "id": account_id},
                    )
            else:
                if state["kind"] == "pending":
                    expiry, interval, next_poll = _grok_pending_state(state)
                    if expiry <= time.time():
                        self.secrets.write(
                            account_id, encode_grok_state({"kind": "failed", "reason": "expired"})
                        )
                        raise GrokConnectionExpired("Grok device login expired")
                    wait_seconds = ceil(next_poll - time.time())
                    if wait_seconds > 0:
                        raise GrokConnectionPending(interval_seconds=wait_seconds)
                    # A device grant can be consumed exactly once. Save this
                    # state before the provider call so a crash cannot replay it.
                    self.secrets.write(account_id, encode_grok_state({"kind": "exchanging"}))
                    try:
                        connected = finish_grok_device_login(state, self.settings)
                    except GrokConnectionPending as pending:
                        interval = pending.interval_seconds or interval
                        state["interval_seconds"] = interval
                        state["next_poll_at"] = time.time() + interval
                        self.secrets.write(account_id, encode_grok_state(state))
                        raise GrokConnectionPending(interval_seconds=interval) from None
                    except GrokConnectionExpired:
                        self.secrets.write(
                            account_id, encode_grok_state({"kind": "failed", "reason": "expired"})
                        )
                        raise
                    except GrokConnectionDenied:
                        self.secrets.write(
                            account_id, encode_grok_state({"kind": "failed", "reason": "denied"})
                        )
                        raise
                    except GrokCredentialTooLarge:
                        self.secrets.write(
                            account_id,
                            encode_grok_state({"kind": "failed", "reason": "credential_too_large"}),
                        )
                        raise
                    except GrokConnectionError:
                        self.secrets.write(account_id, encode_grok_state({"kind": "unknown"}))
                        raise
                    _grok_connected_expiry(connected)
                    connected = dict(connected)
                    connected["credential_revision"] = int(row[2]) + 1
                    try:
                        encoded = encode_grok_state(connected)
                    except GrokCredentialTooLarge:
                        self.secrets.write(
                            account_id,
                            encode_grok_state({"kind": "failed", "reason": "credential_too_large"}),
                        )
                        raise
                    self.secrets.write(account_id, encoded)
                elif state["kind"] == "connected":
                    _grok_connected_expiry(state)  # The file survived a database transaction failure.
                    state["credential_revision"] = int(row[2]) + 1
                    self.secrets.write(account_id, encode_grok_state(state))
                elif state["kind"] in {"exchanging", "unknown"}:
                    raise GrokConnectionUnknown("Grok token exchange outcome is unknown")
                elif state.get("reason") == "expired":
                    raise GrokConnectionExpired("Grok device login expired")
                elif state.get("reason") == "denied":
                    raise GrokConnectionDenied("Grok device login was denied")
                elif state.get("reason") == "credential_too_large":
                    raise GrokCredentialTooLarge(
                        "Grok credential exceeds protected storage capacity; connection not saved"
                    )
                else:
                    raise GrokConnectionError("Grok sign-in failed; start fresh sign-in on this account")
                connection.execute(
                    text(
                        "UPDATE model_accounts SET credential_ready=true,catalog_valid=false,"
                        "credential_revision=credential_revision+1 "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": account_id},
                )
        result = self._account(project_id, account_id)
        assert result is not None
        return result

    def restart_grok(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if (
            account["provider"] != "xai"
            or account["auth_kind"] != "account_runtime"
            or account["credential_present"]
        ):
            raise ValueError("account has no pending Grok sign-in")
        challenge = start_grok_device_login(self.settings)
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT credential_ready FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='xai' "
                    "AND auth_kind='account_runtime' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None:
                raise CredentialChanged()
            if _grok_has_connected_secret(self.secrets, account_id):
                raise ValueError("Grok tokens were saved; complete the existing connection")
            self.secrets.write(
                account_id,
                encode_grok_state(_new_grok_pending(challenge)),
            )
            connection.execute(
                text(
                    "UPDATE model_accounts SET credential_ready=false,catalog_valid=false,"
                    "credential_revision=credential_revision+1 "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": account_id},
            )
        return self.grok_challenge(project_id, account_id)

    def reconnect_grok(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if (
            account["provider"] != "xai"
            or account["auth_kind"] != "account_runtime"
            or not account["credential_present"]
        ):
            raise ValueError("account has no connected Grok sign-in")
        # Obtain a new device challenge before changing the active account.
        challenge = start_grok_device_login(self.settings)
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT credential_ready FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='xai' "
                    "AND auth_kind='account_runtime' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None or not row[0] or not _grok_has_connected_secret(self.secrets, account_id):
                raise CredentialChanged()
            self.secrets.write(account_id, encode_grok_state(_new_grok_pending(challenge)))
            connection.execute(
                text(
                    "UPDATE model_accounts SET credential_ready=false,catalog_valid=false,"
                    "credential_revision=credential_revision+1 "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": account_id},
            )
        return self.grok_challenge(project_id, account_id)

    def codex_challenge(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if (
            account["auth_kind"] != "account_runtime"
            or account["provider"] != "openai_codex_runtime"
            or account["credential_present"]
        ):
            raise ValueError("Codex account has no pending login")
        state = decode_state(self.secrets.read(account_id))
        if state.get("kind") == "exchanged":
            raise CodexConnectionError("Codex tokens were received; complete the saved connection")
        if state.get("kind") != "pending" or int(state.get("expires_at", 0)) <= time.time():
            raise CodexConnectionError("Codex sign-in code expired; start fresh sign-in on this account")
        return {
            "account": account,
            "verification_url": VERIFY_URL,
            "user_code": state["user_code"],
            "expires_at": state["expires_at"],
            "interval_seconds": state["interval_seconds"],
        }

    def grok_challenge(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        if account["provider"] != "xai" or account["credential_present"]:
            raise ValueError("Grok account has no pending login")
        state = decode_grok_state(self.secrets.read(account_id))
        if state["kind"] in {"exchanging", "unknown"}:
            raise GrokConnectionUnknown(
                "Grok token exchange outcome is unknown; start fresh sign-in on this account"
            )
        if state["kind"] == "connected":
            raise GrokConnectionError("Grok account has saved tokens; complete the saved connection")
        if state.get("kind") != "pending":
            raise GrokConnectionError("Grok sign-in expired or failed; start fresh sign-in on this account")
        expiry, interval, _ = _grok_pending_state(state)
        if expiry <= time.time():
            raise GrokConnectionExpired("Grok sign-in expired; start fresh sign-in on this account")
        return {
            "account": account,
            "verification_url": state["verification_url"],
            "user_code": state["user_code"],
            "expires_at": expiry,
            "interval_seconds": interval,
        }

    def codex_credential(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT auth_kind,credential_ready FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='openai_codex_runtime' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None or row[0] != "account_runtime" or not row[1]:
                raise CodexConnectionError("Codex account is not connected")
            state = decode_state(self.secrets.read(account_id))
            if state["kind"] != "connected":
                raise CodexConnectionError("Codex account is not connected")
            if state.get("reconnect_required"):
                raise CodexConnectionError(
                    "Codex login requires reconnection; reconnect account in AI connections"
                )
            expires_at = state.get("expires_at")
            if isinstance(expires_at, int) and expires_at <= time.time() + 60:
                try:
                    state = refresh_credential(state, self.settings)
                except CodexTokenExchangeError as error:
                    if error.status in {400, 401, 403}:
                        self.secrets.write(account_id, encode_state({**state, "reconnect_required": True}))
                    raise
                self.secrets.write(account_id, encode_state(state))
                connection.execute(
                    text(
                        "UPDATE model_accounts SET credential_revision=credential_revision+1 "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": account_id},
                )
            return state

    def grok_credential(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        failure: GrokConnectionError | None = None
        credential: dict[str, Any] | None = None
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT auth_kind,credential_ready,credential_revision FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id AND provider='xai' FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None or row[0] != "account_runtime" or not row[1]:
                raise GrokConnectionError("Grok account is not connected")
            state = decode_grok_state(self.secrets.read(account_id))
            if state["kind"] == "refreshing":
                # An interrupted rotating refresh may have consumed its token.
                failure = GrokConnectionUnknown("Grok token renewal outcome is unknown; sign in again")
                self.secrets.write(account_id, encode_grok_state({"kind": "unknown"}))
                connection.execute(
                    text(
                        "UPDATE model_accounts SET credential_ready=false,catalog_valid=false,"
                        "credential_revision=credential_revision+1 "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": account_id},
                )
            elif state["kind"] != "connected":
                raise GrokConnectionError("Grok account is not connected")
            elif _grok_connected_expiry(state) <= time.time() + 60:
                self.secrets.write(account_id, encode_grok_state({"kind": "refreshing"}))
                try:
                    credential = refresh_grok_credential(state, self.settings)
                    credential = dict(credential)
                    credential["credential_revision"] = int(row[2]) + 1
                    encoded = encode_grok_state(credential)
                except GrokConnectionError as error:
                    failure = error
                    kind = "unknown" if isinstance(error, GrokConnectionUnknown) else "failed"
                    failed_state: dict[str, Any] = {"kind": kind}
                    if isinstance(error, GrokCredentialTooLarge):
                        failed_state["reason"] = "credential_too_large"
                    self.secrets.write(account_id, encode_grok_state(failed_state))
                    connection.execute(
                        text(
                            "UPDATE model_accounts SET credential_ready=false,catalog_valid=false,"
                            "credential_revision=credential_revision+1 "
                            "WHERE project_id=:project_id AND id=:id"
                        ),
                        {"project_id": project_id, "id": account_id},
                    )
                else:
                    self.secrets.write(account_id, encoded)
                    connection.execute(
                        text(
                            "UPDATE model_accounts SET credential_revision=credential_revision+1 "
                            "WHERE project_id=:project_id AND id=:id"
                        ),
                        {"project_id": project_id, "id": account_id},
                    )
            else:
                credential = state
        if failure is not None:
            raise failure
        assert credential is not None
        return credential

    def rotate(self, project_id: UUID, account_id: UUID, data: CredentialPut) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is not None and account["auth_kind"] != "api_key":
            raise ValueError("local account has no credential")
        with self.engine.begin() as connection:
            revision = connection.scalar(
                text(
                    "UPDATE model_accounts SET catalog_valid=false,credential_ready=false, "
                    "credential_revision=credential_revision+1 "
                    "WHERE project_id=:project_id AND id=:id RETURNING credential_revision"
                ),
                {"project_id": project_id, "id": account_id},
            )
        if revision is None:
            return None
        with self.engine.begin() as connection:
            current = connection.scalar(
                text(
                    "SELECT credential_revision FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            )
            if current != revision:
                raise CredentialChanged()
            self.secrets.write(account_id, data.api_key.get_secret_value())
            connection.execute(
                text(
                    "UPDATE model_accounts SET credential_ready=true WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": account_id},
            )
        return self._account(project_id, account_id)

    def remove(self, project_id: UUID, account_id: UUID) -> bool | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text("SELECT id FROM model_accounts WHERE project_id=:project_id AND id=:id FOR UPDATE"),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if row is None:
                return None
            bound = connection.scalar(
                text("SELECT 1 FROM profiles WHERE project_id=:project_id AND model_account_id=:id LIMIT 1"),
                {"project_id": project_id, "id": account_id},
            )
            if bound:
                return False
            connection.execute(
                text("DELETE FROM model_accounts WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": account_id},
            )
        self.secrets.remove(account_id)
        return True

    def catalog(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT c.id,c.account_id,a.provider,c.model_id,c.display_name, "
                        "c.category,c.reasoning_efforts,a.auth_kind,a.catalog_valid,a.credential_ready,"
                        "a.credential_revision "
                        "FROM model_catalog c JOIN model_accounts a "
                        "ON a.project_id=c.project_id AND a.id=c.account_id "
                        "WHERE c.project_id=:project_id "
                        "ORDER BY a.provider,c.category,c.display_name,c.model_id"
                    ),
                    {"project_id": project_id},
                )
                .mappings()
                .all()
            )
        result = []
        for raw in rows:
            row = dict(raw)
            catalog_valid = row.pop("catalog_valid")
            credential_ready = row.pop("credential_ready")
            revision = int(row.pop("credential_revision"))
            auth_kind = row.pop("auth_kind")
            if row["provider"] == "xai" and auth_kind == "account_runtime":
                present = _grok_has_connected_secret(self.secrets, row["account_id"], revision)
            else:
                present = self.secrets.present(row["account_id"])
            if row["provider"] == "openai_codex_runtime" and auth_kind == "account_runtime":
                present = (
                    present
                    and codex_connection_status(self.secrets, row["account_id"], bool(credential_ready))
                    == "connected"
                )
            row["available"] = bool(catalog_valid and (auth_kind == "none" or credential_ready and present))
            result.append(row)
        return result

    def refresh(self, project_id: UUID, account_id: UUID) -> dict[str, Any] | None:
        account = self._account(project_id, account_id)
        if account is None:
            return None
        codex_state = (
            self.codex_credential(project_id, account_id)
            if account["provider"] == "openai_codex_runtime"
            else None
        )
        grok_state = self.grok_credential(project_id, account_id) if account["provider"] == "xai" else None
        revision = -1
        generation = -1
        try:
            with self.engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT credential_revision,credential_ready,refresh_generation FROM model_accounts "
                        "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": account_id},
                ).one_or_none()
                if row is None:
                    raise CredentialChanged()
                revision = int(row[0])
                ready = bool(row[1])
                previous_generation = int(row[2])
                if account["auth_kind"] == "api_key" and not ready:
                    raise SecretStoreError("model credential unavailable")
                api_key = self.secrets.read(account_id) if account["auth_kind"] == "api_key" else ""
                generation = previous_generation + 1
                connection.execute(
                    text(
                        "UPDATE model_accounts SET refresh_generation=:generation "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": account_id, "generation": generation},
                )
            if account["provider"] == "openai_codex_runtime":
                assert codex_state is not None
                models = discover_codex_models(codex_state, self.settings)
            elif account["provider"] == "xai":
                assert grok_state is not None
                models = discover_grok_models(grok_state, self.settings)
            else:
                models = discover_models(account["provider"], api_key, self.settings, account["endpoint"])
        except SecretStoreError:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE model_accounts SET catalog_valid=false "
                        "WHERE project_id=:project_id AND id=:id AND credential_revision=:revision"
                    ),
                    {"project_id": project_id, "id": account_id, "revision": revision},
                )
            raise
        except (DiscoveryError, TransportError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE model_accounts SET catalog_valid=false "
                        "WHERE project_id=:project_id AND id=:id "
                        "AND credential_revision=:revision AND refresh_generation=:generation"
                    ),
                    {
                        "project_id": project_id,
                        "id": account_id,
                        "revision": revision,
                        "generation": generation,
                    },
                )
            raise
        with self.engine.begin() as connection:
            current = connection.execute(
                text(
                    "SELECT credential_revision,refresh_generation FROM model_accounts "
                    "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                ),
                {"project_id": project_id, "id": account_id},
            ).one_or_none()
            if current != (revision, generation):
                raise CredentialChanged()
            connection.execute(
                text("DELETE FROM model_catalog WHERE project_id=:project_id AND account_id=:id"),
                {"project_id": project_id, "id": account_id},
            )
            for model in models:
                connection.execute(
                    text(
                        "INSERT INTO model_catalog(id,project_id,actor_id,account_id, "
                        "model_id,display_name,category,reasoning_efforts) "
                        "VALUES (:id,:project_id,:actor_id,:account_id,:model_id,:display_name,"
                        ":category,:efforts)"
                    ).bindparams(bindparam("efforts", type_=JSONB)),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "account_id": account_id,
                        "model_id": model.model_id,
                        "display_name": model.display_name,
                        "category": model.category,
                        "efforts": list(model.reasoning_efforts),
                    },
                )
            connection.execute(
                text(
                    "UPDATE model_accounts SET catalog_valid=true,last_refreshed_at=now() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": account_id},
            )
        refreshed = self._account(project_id, account_id)
        assert refreshed is not None
        return {
            "account": refreshed,
            "models": [row for row in self.catalog(project_id) if row["account_id"] == account_id],
        }


def make_model_router(gateway: ModelGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/model")
    router.include_router(make_codex_recovery_router(gateway))

    @router.get("/providers", response_model=list[ProviderOut])
    def providers(project_id: UUID) -> list[ProviderOut]:
        try:
            if not gateway.project_exists(project_id):
                raise HTTPException(404, "project not found")
        except SQLAlchemyError:
            raise HTTPException(503, "model account store unavailable") from None
        return [
            ProviderOut(
                id=spec.id,
                name=spec.name,
                auth_kind="none"
                if spec.id == "ollama"
                else "account_runtime"
                if spec.id in {"openai_codex_runtime", "xai"}
                else "api_key",
                catalog_supported=spec.catalog_supported,
                text_runtime_available=spec.text_runtime_available,
                connectable=spec.connectable
                and (spec.id != "xai" or bool(gateway.settings.xai_oauth_client_id)),
            )
            for spec in provider_directory()
        ]

    @router.get("/accounts", response_model=list[AccountOut])
    def accounts(project_id: UUID) -> list[AccountOut]:
        try:
            if not gateway.project_exists(project_id):
                raise HTTPException(404, "project not found")
            return [AccountOut.model_validate(item) for item in gateway.list_accounts(project_id)]
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None

    @router.post("/accounts", response_model=AccountOut, status_code=201)
    def create(project_id: UUID, data: AccountCreate) -> AccountOut:
        if data.provider in {"openai_codex_runtime", "xai"}:
            raise HTTPException(422, "Use account device login")
        if data.provider == "ollama":
            if data.api_key is not None or data.endpoint is None:
                raise HTTPException(422, "invalid local model account")
            try:
                validate_local_endpoint(data.endpoint)
            except ValueError:
                raise HTTPException(422, "invalid local model endpoint") from None
        elif data.api_key is None or data.endpoint is not None:
            raise HTTPException(422, "invalid model API account")
        else:
            require_valid_key(data.api_key)
        if not data.label.strip():
            raise HTTPException(422, "model account label must not be blank")
        try:
            if not gateway.project_exists(project_id):
                raise HTTPException(404, "project not found")
            return AccountOut.model_validate(gateway.create(project_id, data))
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None

    @router.post("/codex/start", response_model=CodexStartOut, status_code=201)
    def codex_start(project_id: UUID, data: CodexStart) -> CodexStartOut:
        try:
            if not gateway.project_exists(project_id):
                raise HTTPException(404, "project not found")
            return CodexStartOut.model_validate(gateway.start_codex(project_id, data.label))
        except ValueError:
            raise HTTPException(422, "model account label must not be blank") from None
        except CodexDeviceStartError as error:
            raise HTTPException(
                502, f"OpenAI sign-in endpoint returned HTTP {error.status}; no connection saved"
            ) from None
        except (CodexConnectionError, DiscoveryError, TransportError):
            raise HTTPException(502, "Codex device login unavailable") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None

    @router.post("/accounts/{account_id}/codex/finish", response_model=AccountOut)
    def codex_finish(project_id: UUID, account_id: UUID) -> AccountOut:
        try:
            result = gateway.finish_codex(project_id, account_id)
        except CodexConnectionPending:
            raise HTTPException(409, "Complete Codex sign-in, then check again") from None
        except CodexTokenExchangeError as error:
            raise HTTPException(
                502,
                f"OpenAI rejected the Codex token exchange (HTTP {error.status}). "
                "Start fresh sign-in on this account.",
            ) from None
        except CodexTokenExchangeUnknown:
            raise HTTPException(
                502,
                "OpenAI token exchange did not return a response. Its outcome is unknown; "
                "start fresh sign-in on this account.",
            ) from None
        except CodexConnectionError as error:
            raise HTTPException(409, str(error)) from None
        except TransportError:
            raise HTTPException(502, "OpenAI device login transport failed") from None
        except (ValueError, CredentialChanged):
            raise HTTPException(409, "account is not a Codex connection") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return AccountOut.model_validate(result)

    @router.post("/accounts/{account_id}/codex/restart", response_model=CodexStartOut)
    def codex_restart(project_id: UUID, account_id: UUID) -> CodexStartOut:
        try:
            result = gateway.restart_codex(project_id, account_id)
        except CodexDeviceStartError as error:
            raise HTTPException(
                502, f"OpenAI sign-in endpoint returned HTTP {error.status}; code unchanged"
            ) from None
        except CodexConnectionError as error:
            raise HTTPException(409, str(error)) from None
        except (DiscoveryError, TransportError):
            raise HTTPException(502, "Codex device login unavailable; code unchanged") from None
        except (ValueError, CredentialChanged):
            raise HTTPException(409, "account has no pending Codex sign-in") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return CodexStartOut.model_validate(result)

    @router.get("/accounts/{account_id}/codex/challenge", response_model=CodexStartOut)
    def codex_challenge(project_id: UUID, account_id: UUID) -> CodexStartOut:
        try:
            result = gateway.codex_challenge(project_id, account_id)
        except CodexConnectionError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError:
            raise HTTPException(
                409, "Codex sign-in code expired; start fresh sign-in on this account"
            ) from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return CodexStartOut.model_validate(result)

    @router.post("/grok/start", response_model=CodexStartOut, status_code=201)
    def grok_start(project_id: UUID, data: GrokStart) -> CodexStartOut:
        if not gateway.settings.xai_oauth_client_id:
            raise HTTPException(409, "Grok account login needs XAI_OAUTH_CLIENT_ID in OneFoundry .env")
        try:
            if not gateway.project_exists(project_id):
                raise HTTPException(404, "project not found")
            return CodexStartOut.model_validate(gateway.start_grok(project_id, data.label))
        except ValueError:
            raise HTTPException(422, "model account label must not be blank") from None
        except GrokConnectionError:
            raise HTTPException(502, "Grok device login unavailable") from None
        except (DiscoveryError, TransportError):
            raise HTTPException(502, "Grok device login unavailable") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None

    @router.post("/accounts/{account_id}/grok/finish", response_model=AccountOut)
    def grok_finish(project_id: UUID, account_id: UUID) -> AccountOut:
        try:
            result = gateway.finish_grok(project_id, account_id)
        except GrokConnectionPending as pending:
            headers = {"Retry-After": str(pending.interval_seconds)} if pending.interval_seconds else None
            raise HTTPException(409, "Complete Grok sign-in, then check again", headers=headers) from None
        except GrokConnectionExpired:
            raise HTTPException(409, "Grok sign-in expired; start fresh sign-in on this account") from None
        except GrokConnectionDenied:
            raise HTTPException(409, "Grok sign-in was denied; start fresh sign-in on this account") from None
        except GrokCredentialTooLarge:
            raise HTTPException(
                409, "Grok credential exceeds protected storage capacity; connection not saved"
            ) from None
        except GrokConnectionUnknown:
            raise HTTPException(
                502, "Grok token exchange outcome is unknown; start fresh sign-in on this account"
            ) from None
        except (GrokConnectionError, TransportError):
            raise HTTPException(502, "Grok sign-in failed; start fresh sign-in on this account") from None
        except (ValueError, CredentialChanged):
            raise HTTPException(409, "account is not a Grok subscription connection") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return AccountOut.model_validate(result)

    @router.post("/accounts/{account_id}/grok/restart", response_model=CodexStartOut)
    def grok_restart(project_id: UUID, account_id: UUID) -> CodexStartOut:
        if not gateway.settings.xai_oauth_client_id:
            raise HTTPException(409, "Grok account login needs XAI_OAUTH_CLIENT_ID in OneFoundry .env")
        try:
            result = gateway.restart_grok(project_id, account_id)
        except (GrokConnectionError, DiscoveryError, TransportError):
            raise HTTPException(502, "Grok device login unavailable; code unchanged") from None
        except (ValueError, CredentialChanged):
            raise HTTPException(409, "account has no pending Grok sign-in") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return CodexStartOut.model_validate(result)

    @router.post("/accounts/{account_id}/grok/reconnect", response_model=CodexStartOut)
    def grok_reconnect(project_id: UUID, account_id: UUID) -> CodexStartOut:
        if not gateway.settings.xai_oauth_client_id:
            raise HTTPException(409, "Grok account login needs XAI_OAUTH_CLIENT_ID in OneFoundry .env")
        try:
            result = gateway.reconnect_grok(project_id, account_id)
        except (GrokConnectionError, DiscoveryError, TransportError):
            raise HTTPException(502, "Grok device login unavailable; connection unchanged") from None
        except (ValueError, CredentialChanged):
            raise HTTPException(409, "account has no connected Grok sign-in") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return CodexStartOut.model_validate(result)

    @router.get("/accounts/{account_id}/grok/challenge", response_model=CodexStartOut)
    def grok_challenge(project_id: UUID, account_id: UUID) -> CodexStartOut:
        try:
            result = gateway.grok_challenge(project_id, account_id)
        except GrokConnectionError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError:
            raise HTTPException(409, "Grok device login is no longer pending") from None
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return CodexStartOut.model_validate(result)

    @router.put("/accounts/{account_id}/credential", response_model=AccountOut)
    def rotate(project_id: UUID, account_id: UUID, data: CredentialPut) -> AccountOut:
        require_valid_key(data.api_key)
        try:
            item = gateway.rotate(project_id, account_id, data)
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model account store unavailable") from None
        except ValueError:
            raise HTTPException(409, "local account has no credential") from None
        except CredentialChanged:
            raise HTTPException(409, "model credential changed") from None
        if item is None:
            raise HTTPException(404, "model account not found")
        return AccountOut.model_validate(item)

    @router.delete("/accounts/{account_id}", status_code=204)
    def remove(project_id: UUID, account_id: UUID) -> Response:
        try:
            result = gateway.remove(project_id, account_id)
        except (SQLAlchemyError, SecretStoreError, IntegrityError):
            raise HTTPException(503, "model account store unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        if result is False:
            raise HTTPException(409, "model account is assigned to a profile")
        return Response(status_code=204)

    @router.get("/catalog", response_model=list[CatalogModelOut])
    def catalog(project_id: UUID) -> list[CatalogModelOut]:
        try:
            if not gateway.project_exists(project_id):
                raise HTTPException(404, "project not found")
            return [CatalogModelOut.model_validate(item) for item in gateway.catalog(project_id)]
        except (SQLAlchemyError, SecretStoreError):
            raise HTTPException(503, "model catalog unavailable") from None

    @router.post("/accounts/{account_id}/refresh", response_model=RefreshOut)
    def refresh(project_id: UUID, account_id: UUID) -> RefreshOut:
        try:
            result = gateway.refresh(project_id, account_id)
        except SecretStoreError:
            raise HTTPException(409, "model credential unavailable") from None
        except CodexModelDiscoveryError as error:
            raise HTTPException(502, str(error)) from None
        except DiscoveryError:
            raise HTTPException(502, "provider model discovery failed") from None
        except TransportError:
            raise HTTPException(502, "provider model discovery transport failed") from None
        except GrokCredentialTooLarge:
            raise HTTPException(
                409, "Grok credential exceeds protected storage capacity; connection not saved"
            ) from None
        except (CodexConnectionError, GrokConnectionError):
            raise HTTPException(409, "account needs reconnection") from None
        except CredentialChanged:
            raise HTTPException(409, "model refresh superseded by another operation") from None
        except SQLAlchemyError:
            raise HTTPException(503, "model catalog unavailable") from None
        if result is None:
            raise HTTPException(404, "model account not found")
        return RefreshOut.model_validate(result)

    return router
