"""Operator-registered Microsoft and Google accounts with tokens in the protected store.

The database keeps only identity, status, features, and granted scopes. Client
secrets, pending sign-in material, and tokens live in the ``connectors``
subdirectory of the model secret root and never reach the API, events, or models.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import connector_oauth
from onefoundry.connector_oauth import OAuthError
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.inference.http import HTTPTransport, send_http
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError

if TYPE_CHECKING:
    from onefoundry.connector_schemas import ConnectorCreate
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

COLUMNS = (
    "id,provider,label,client_id,tenant,features,status,last_error_code,granted_scopes,connected_at,"
    "version,created_at"
)


_REFRESH_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[UUID, threading.Lock] = {}


def _refresh_lock(account_id: UUID) -> threading.Lock:
    with _REFRESH_GUARD:
        return _REFRESH_LOCKS.setdefault(account_id, threading.Lock())


class ConnectorStore:
    def __init__(self, gateway: ToolGateway, settings: Settings, http: HTTPTransport = send_http) -> None:
        self.gateway = gateway
        self.settings = settings
        self.http = http
        self.secrets = ModelSecretStore(str(Path(settings.model_secret_root) / "connectors"))

    def prepare_secrets(self) -> None:
        ModelSecretStore(self.settings.model_secret_root).check_ready()
        self.secrets.root.mkdir(mode=0o700, exist_ok=True)
        self.secrets.check_ready()

    def redirect_uri(self) -> str:
        return (
            self.settings.connector_google_redirect_uri
            or f"http://127.0.0.1:{self.settings.app_public_port}/"
        )

    def _vault(self, account_id: UUID) -> dict[str, Any]:
        try:
            value = json.loads(base64.urlsafe_b64decode(self.secrets.read(account_id)))
        except (SecretStoreError, ValueError):
            raise Unavailable("connector credential is unavailable; reconnect the account") from None
        if not isinstance(value, dict):
            raise Unavailable("connector credential is unavailable; reconnect the account")
        return value

    def _save(self, account_id: UUID, vault: dict[str, Any]) -> None:
        try:
            # The store holds printable ASCII without spaces, so the JSON vault is base64url encoded.
            encoded = base64.urlsafe_b64encode(json.dumps(vault, separators=(",", ":")).encode()).decode()
            self.secrets.write(account_id, encoded)
        except SecretStoreError:
            raise Unavailable("connector credential store is unavailable") from None

    def _row(
        self, connection: Any, project_id: UUID, account_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    f"SELECT {COLUMNS} FROM connector_accounts WHERE project_id=:p AND id=:id"  # noqa: S608
                    + (" FOR UPDATE" if lock else "")
                ),
                {"p": project_id, "id": account_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("connected account not found")
        public = dict(row)
        public["features"] = list(public["features"])
        return public

    def list(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            ids: Sequence[UUID] = (
                connection.execute(
                    text("SELECT id FROM connector_accounts WHERE project_id=:p ORDER BY created_at,id"),
                    {"p": project_id},
                )
                .scalars()
                .all()
            )
            return [self._row(connection, project_id, account_id) for account_id in ids]

    def create(self, project_id: UUID, data: ConnectorCreate) -> dict[str, Any]:
        account_id = uuid4()
        secret = data.client_secret.get_secret_value() if data.client_secret else None
        if data.provider == "google" and not secret:
            raise Conflict("Google desktop app sign-in needs the client secret from the same OAuth client")
        self._save(account_id, {"client_secret": secret})
        try:
            with self.gateway.engine.begin() as connection:
                inserted = connection.execute(
                    text(
                        "INSERT INTO connector_accounts "
                        "(id,project_id,actor_id,provider,label,client_id,tenant,features) "
                        "VALUES (:id,:p,:actor,:provider,:label,:client,:tenant,:features) "
                        "ON CONFLICT (project_id,label) DO NOTHING"
                    ).bindparams(bindparam("features", type_=JSONB)),
                    {
                        "id": account_id,
                        "p": project_id,
                        "actor": self.gateway.actor_id,
                        "provider": data.provider,
                        "label": data.label,
                        "client": data.client_id,
                        "tenant": data.tenant if data.provider == "microsoft" else "common",
                        "features": sorted(set(data.features)),
                    },
                )
                if inserted.rowcount != 1:
                    raise Conflict("a connected account with this label already exists")
                self.gateway._event(
                    connection,
                    project_id,
                    None,
                    None,
                    "connector_account_saved",
                    {
                        "account_id": str(account_id),
                        "provider": data.provider,
                        "features": sorted(set(data.features)),
                    },
                )
                return self._row(connection, project_id, account_id)
        except Exception:
            self.secrets.remove(account_id)
            raise

    def start(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, account_id)
        vault = self._vault(account_id)
        scope = connector_oauth.scopes(row["provider"], row["features"])
        timeout = float(self.settings.connector_http_timeout_seconds)
        if row["provider"] == "microsoft":
            try:
                device = connector_oauth.microsoft_start(
                    row["client_id"], row["tenant"], scope, http=self.http, timeout=timeout
                )
            except OAuthError as failure:
                self._fail(project_id, account_id, failure.code)
                raise Conflict(f"Microsoft sign-in could not start ({failure.code})") from None
            vault["pending"] = {
                "kind": "device",
                "device_code": device.device_code,
                "expires_at": device.expires_at,
            }
            self._save(account_id, vault)
            return {
                "kind": "device",
                "user_code": device.user_code,
                "verification_uri": device.verification_uri,
                "expires_at": device.expires_at,
                "interval": device.interval,
            }
        verifier, challenge = connector_oauth.pkce_pair()
        state = f"{account_id}.{secrets.token_urlsafe(24)}"
        vault["pending"] = {
            "kind": "redirect",
            "verifier": verifier,
            "state": state,
            "expires_at": int(time.time()) + 900,
        }
        self._save(account_id, vault)
        return {
            "kind": "redirect",
            "authorize_url": connector_oauth.google_authorize_url(
                row["client_id"], self.redirect_uri(), scope, state, challenge
            ),
        }

    def poll(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, account_id)
        vault = self._vault(account_id)
        pending = vault.get("pending") or {}
        if row["provider"] != "microsoft" or pending.get("kind") != "device":
            raise Conflict("no device sign-in is waiting for this account")
        if int(pending.get("expires_at", 0)) < time.time():
            vault.pop("pending", None)
            self._save(account_id, vault)
            self._fail(project_id, account_id, "sign_in_expired")
            return self.get(project_id, account_id)
        try:
            tokens = connector_oauth.microsoft_poll(
                row["client_id"],
                row["tenant"],
                str(pending["device_code"]),
                http=self.http,
                timeout=float(self.settings.connector_http_timeout_seconds),
            )
        except OAuthError as failure:
            if failure.code == "transport_failed":
                raise Unavailable("Microsoft sign-in could not be checked; try again") from None
            vault.pop("pending", None)
            self._save(account_id, vault)
            self._fail(project_id, account_id, failure.code)
            return self.get(project_id, account_id)
        if tokens is None:
            return self.get(project_id, account_id)
        self._connect(project_id, account_id, vault, tokens)
        return self.get(project_id, account_id)

    def callback(self, state: str, code: str) -> dict[str, Any]:
        account_text, _, _ = state.partition(".")
        try:
            account_id = UUID(account_text)
        except ValueError:
            raise NotFound("sign-in request not found") from None
        with self.gateway.engine.connect() as connection:
            project_id = connection.scalar(
                text("SELECT project_id FROM connector_accounts WHERE id=:id AND provider='google'"),
                {"id": account_id},
            )
            if project_id is None:
                raise NotFound("sign-in request not found")
            row = self._row(connection, project_id, account_id)
        vault = self._vault(account_id)
        pending = vault.get("pending") or {}
        if (
            pending.get("kind") != "redirect"
            or not secrets.compare_digest(str(pending.get("state", "")), state)
            or int(pending.get("expires_at", 0)) < time.time()
        ):
            raise Conflict("this sign-in link is no longer valid; start sign-in again")
        try:
            tokens = connector_oauth.google_exchange(
                row["client_id"],
                str(vault.get("client_secret") or ""),
                code,
                str(pending["verifier"]),
                self.redirect_uri(),
                http=self.http,
                timeout=float(self.settings.connector_http_timeout_seconds),
            )
        except OAuthError as failure:
            vault.pop("pending", None)
            self._save(account_id, vault)
            self._fail(project_id, account_id, failure.code)
            raise Conflict(f"Google sign-in failed ({failure.code})") from None
        self._connect(project_id, account_id, vault, tokens)
        return self.get(project_id, account_id)

    def get(self, project_id: UUID, account_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            return self._row(connection, project_id, account_id)

    def _connect(
        self, project_id: UUID, account_id: UUID, vault: dict[str, Any], tokens: dict[str, Any]
    ) -> None:
        vault.pop("pending", None)
        vault["tokens"] = tokens
        self._save(account_id, vault)
        with self.gateway.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status='connected',last_error_code=NULL,"
                    "granted_scopes=:scope,"
                    "connected_at=clock_timestamp(),version=version+1 WHERE project_id=:p AND id=:id"
                ),
                {"scope": str(tokens.get("scope", ""))[:2000], "p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "connector_account_connected",
                {"account_id": str(account_id)},
            )

    def _fail(self, project_id: UUID, account_id: UUID, code: str) -> None:
        with self.gateway.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status='failed',last_error_code=:e,version=version+1 "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"e": code, "p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "connector_account_failed",
                {"account_id": str(account_id), "error_code": code},
            )

    def access_token(self, project_id: UUID, account_id: UUID) -> str:
        """A current access token, refreshed and saved when it is about to expire."""
        with self.gateway.engine.connect() as connection:
            row = self._row(connection, project_id, account_id)
        if row["status"] != "connected":
            raise Unavailable("the connected account needs to sign in again")
        tokens = self._vault(account_id).get("tokens") or {}
        if int(tokens.get("expires_at", 0)) > time.time() + 60 and tokens.get("access_token"):
            return str(tokens["access_token"])
        # API requests and the launcher share this process; one refresh per account at a time,
        # re-reading the vault so a second caller uses the token the first one just saved.
        with _refresh_lock(account_id):
            return self._refresh(project_id, account_id, row)

    def _refresh(self, project_id: UUID, account_id: UUID, row: dict[str, Any]) -> str:
        vault = self._vault(account_id)
        tokens = vault.get("tokens") or {}
        if int(tokens.get("expires_at", 0)) > time.time() + 60 and tokens.get("access_token"):
            return str(tokens["access_token"])
        if not tokens.get("refresh_token"):
            self._fail(project_id, account_id, "refresh_missing")
            raise Unavailable("the connected account needs to sign in again")
        try:
            fresh = connector_oauth.refresh(
                row["provider"],
                row["client_id"],
                row["tenant"],
                vault.get("client_secret"),
                str(tokens["refresh_token"]),
                http=self.http,
                timeout=float(self.settings.connector_http_timeout_seconds),
            )
        except OAuthError as failure:
            if failure.code == "refresh_rejected":
                self._fail(project_id, account_id, failure.code)
            raise Unavailable(
                f"the connected account could not refresh its sign-in ({failure.code})"
            ) from None
        vault["tokens"] = fresh
        self._save(account_id, vault)
        return str(fresh["access_token"])

    def disconnect(self, project_id: UUID, account_id: UUID, expected: int) -> None:
        with self.gateway.engine.begin() as connection:
            row = self._row(connection, project_id, account_id, lock=True)
            if row["version"] != expected:
                raise Conflict("connected account changed; reload before removing")
            if connection.scalar(
                text(
                    "SELECT 1 FROM document_sources WHERE project_id=:p AND connector_account_id=:id LIMIT 1"
                ),
                {"p": project_id, "id": account_id},
            ):
                raise Conflict("remove the watched folders that use this account first")
            if connection.scalar(
                text("SELECT 1 FROM mail_accounts WHERE project_id=:p AND connector_account_id=:id LIMIT 1"),
                {"p": project_id, "id": account_id},
            ):
                raise Conflict("remove the mailboxes that use this account first")
            connection.execute(
                text("DELETE FROM connector_accounts WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": account_id},
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "connector_account_removed",
                {"account_id": str(account_id)},
            )
        if row["provider"] == "google":
            try:
                tokens = self._vault(account_id).get("tokens") or {}
            except Unavailable:
                tokens = {}
            if tokens.get("refresh_token"):
                connector_oauth.google_revoke(str(tokens["refresh_token"]), http=self.http)
        self.secrets.remove(account_id)
