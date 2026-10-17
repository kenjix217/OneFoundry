"""Gateway-owned, single-use sign-in and serialized protected token renewal."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.zoho_mail_contracts import ZohoEndpoint
from onefoundry.zoho_mail_mcp import ZohoMcpError
from onefoundry.zoho_mail_oauth import BoundedOAuthHTTP, ZohoOAuth, ZohoOAuthError
from onefoundry.zoho_mail_store import ZohoMailStore


class ZohoSignInStore:
    def __init__(
        self, store: ZohoMailStore, oauth: ZohoOAuth | None = None, clock: Callable[[], float] | None = None
    ) -> None:
        self.store, self.clock = store, clock or store.clock
        self.oauth = oauth or ZohoOAuth(
            BoundedOAuthHTTP(
                float(store.settings.connector_http_timeout_seconds), store.settings.mail_max_message_bytes
            ),
            store.settings.zoho_oauth_authority_hosts,
        )

    def _active(self, connection: Any, project_id: UUID) -> None:
        self.store.gateway._require_project(connection, project_id)
        active = connection.scalar(
            text("SELECT id FROM projects WHERE id=:p AND status='active' FOR SHARE"), {"p": project_id}
        )
        if active is None:
            raise Conflict("Zoho sign-in requires an active project")

    def _event(self, connection: Any, row: dict[str, Any], kind: str, code: str | None = None) -> None:
        value = {"connection_id": str(row["id"])}
        if code:
            value["error_code"] = code
        self.store.gateway._event(connection, row["project_id"], None, None, kind, value)

    def endpoint(self, project_id: UUID, connection_id: UUID, data: ZohoEndpoint) -> dict[str, Any]:
        with self.store.gateway.engine.begin() as connection:
            self._active(connection, project_id)
            row = self.store.row(connection, project_id, connection_id, lock=True)
            if row["version"] != data.expected_version:
                raise Conflict("Zoho connection changed; refresh before saving its server")
            changed = self.store.replace_authorization(
                connection,
                row,
                {
                    "server_url": data.server_url.get_secret_value(),
                    "mail_account_id": data.mail_account_id,
                },
                "zoho_mail_endpoint_saved",
            )
            return self.store.public(changed)

    def start(self, project_id: UUID, connection_id: UUID, expected_version: int) -> dict[str, str]:
        with self.store.gateway.engine.begin() as connection:
            self._active(connection, project_id)
            row = self.store.row(connection, project_id, connection_id, lock=True)
            if row["version"] != expected_version:
                raise Conflict("Zoho connection changed; refresh before connecting")
            value = self.store.protected(row)
            value.pop("access_token", None)
            value.pop("refresh_token", None)
            value.pop("expires_at", None)
            value["pending"] = {"phase": "preparing"}
            claimed = self.store.replace_authorization(connection, row, value, "zoho_mail_signin_started")
        try:
            metadata = self.oauth.discover(value["server_url"])
            redirect = (
                f"http://127.0.0.1:{self.store.settings.app_public_port}/api/projects/{project_id}"
                f"/mail/zoho/connections/{connection_id}/oauth/callback"
            )
            client = value.get("oauth_client")
            if (
                not isinstance(client, dict)
                or client.get("redirect_uri") != redirect
                or value.get("oauth") != metadata
            ):
                client = self.oauth.register(value["server_url"], metadata, redirect)
            value["oauth"], value["oauth_client"] = metadata, client
            value["pending"] = self.oauth.pending(
                metadata, client, self.clock() + self.store.settings.zoho_oauth_pending_timeout_seconds
            )
            with self.store.gateway.engine.begin() as connection:
                self._active(connection, project_id)
                current = self.store.row(connection, project_id, connection_id, lock=True)
                self._same_claim(current, claimed)
                self.store.write_protected(connection, current, value)
                self._event(connection, current, "zoho_mail_signin_ready")
        except ZohoOAuthError as error:
            self._failed(claimed, error.code)
            raise Conflict("Zoho sign-in could not start; reconnect from the Zoho Mail screen") from None
        return {
            "authorize_path": (
                f"/api/projects/{project_id}/mail/zoho/connections/{connection_id}/oauth/authorize"
            )
        }

    @staticmethod
    def _same_claim(current: dict[str, Any], claimed: dict[str, Any]) -> None:
        if (
            current["version"] != claimed["version"]
            or current["credential_revision"] != claimed["credential_revision"]
        ):
            raise Conflict("This Zoho sign-in changed; connect again")

    def authorize(self, project_id: UUID, connection_id: UUID) -> str:
        with self.store.gateway.engine.connect() as connection:
            row = self.store.row(connection, project_id, connection_id)
            value = self.store.protected(row)
        pending = value.get("pending", {})
        if pending.get("phase") != "waiting" or pending.get("expires_at", 0) <= self.clock():
            raise Conflict("This Zoho sign-in expired; connect again")
        return str(pending["authorize_url"])

    def callback(
        self,
        project_id: UUID,
        connection_id: UUID,
        state: str,
        code: str | None,
        denied: bool = False,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        with self.store.gateway.engine.begin() as connection:
            self._active(connection, project_id)
            row = self.store.row(connection, project_id, connection_id, lock=True)
            value = self.store.protected(row)
            pending = value.get("pending", {})
            metadata = value.get("oauth", {})
            if (
                pending.get("phase") != "waiting"
                or pending.get("expires_at", 0) <= self.clock()
                or not state
                or not secrets.compare_digest(str(pending.get("state", "")), state)
                or (issuer is not None and issuer != metadata.get("issuer"))
                or (metadata.get("issuer_required") and issuer is None)
            ):
                raise Conflict("This Zoho sign-in is no longer valid; connect again")
            pending["phase"] = "consumed"
            self.store.write_protected(connection, row, value)
            self._event(connection, row, "zoho_mail_signin_callback_claimed")
            claimed = self.store.row(connection, project_id, connection_id)
        if denied or not code or len(code) > 4096:
            self._failed(claimed, "sign_in_declined" if denied else "sign_in_failed")
            raise Conflict("Zoho sign-in was not completed; connect again")
        try:
            tokens = self.oauth.tokens(
                metadata,
                value["oauth_client"],
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": pending["verifier"],
                    "redirect_uri": value["oauth_client"]["redirect_uri"],
                },
            )
            value.pop("pending", None)
            self._put_tokens(value, tokens)
            with self.store.gateway.engine.begin() as connection:
                self._active(connection, project_id)
                current = self.store.row(connection, project_id, connection_id, lock=True)
                self._same_claim(current, claimed)
                self.store.write_protected(connection, current, value)
                connection.execute(
                    text(
                        "UPDATE zoho_mail_connections SET status='unverified',enabled=false,"
                        "last_error_code=NULL "
                        "WHERE id=:id AND project_id=:p"
                    ),
                    {"id": connection_id, "p": project_id},
                )
                self._event(connection, current, "zoho_mail_signin_completed")
        except ZohoOAuthError as error:
            self._failed(claimed, error.code)
            raise Conflict("Zoho sign-in could not be completed; connect again") from None
        return self.store.probe(project_id, connection_id)

    def _put_tokens(self, value: dict[str, Any], tokens: dict[str, Any]) -> None:
        value["access_token"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            value["refresh_token"] = tokens["refresh_token"]
        expiry = tokens["expires_in"]
        value["expires_at"] = self.clock() + expiry if expiry is not None else None

    def _failed(self, claimed: dict[str, Any], code: str) -> None:
        with self.store.gateway.engine.begin() as connection:
            row = self.store.row(connection, claimed["project_id"], claimed["id"], lock=True)
            if (
                row["version"] != claimed["version"]
                or row["credential_revision"] != claimed["credential_revision"]
            ):
                return
            value = self.store.protected(row)
            value.pop("pending", None)
            self.store.write_protected(connection, row, value)
            connection.execute(
                text(
                    "UPDATE zoho_mail_connections SET status='failed',enabled=false,"
                    "last_error_code=:code "
                    "WHERE id=:id AND project_id=:p"
                ),
                {"code": code, "id": row["id"], "p": row["project_id"]},
            )
            self._event(connection, row, "zoho_mail_signin_failed", code)

    def access(self, caller: dict[str, Any]) -> dict[str, Any]:
        failure: str | None = None
        with self.store.gateway.engine.begin() as connection:
            self._active(connection, caller["project_id"])
            row = self.store.row(connection, caller["project_id"], caller["id"], lock=True)
            if row["version"] != caller["version"]:
                raise Conflict("Zoho authorization changed; refresh before continuing")
            value = self.store.vault(row)
            expires = value.get("expires_at")
            if not value.get("oauth") or expires is None or expires > self.clock():
                return value
            if not value.get("refresh_token"):
                failure = "oauth_reconnect_required"
            else:
                try:
                    tokens = self.oauth.tokens(
                        value["oauth"],
                        value["oauth_client"],
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": value["refresh_token"],
                        },
                    )
                    self._put_tokens(value, tokens)
                    self.store.write_protected(connection, row, value)
                    self._event(connection, row, "zoho_mail_oauth_refreshed")
                except ZohoOAuthError as error:
                    failure = error.code
            if failure:
                connection.execute(
                    text(
                        "UPDATE zoho_mail_connections SET status='failed',enabled=false,"
                        "last_error_code=:code "
                        "WHERE id=:id AND project_id=:p"
                    ),
                    {"code": failure, "id": row["id"], "p": row["project_id"]},
                )
                self._event(connection, row, "zoho_mail_oauth_refresh_failed", failure)
        if failure:
            raise ZohoMcpError("authentication_failed")
        return value
