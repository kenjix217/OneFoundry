"""One explicitly approved, durable Microsoft revokeSignInSessions attempt.

The confirmation is the operator's approval for ALL app/browser sessions.
Claiming disables local token use before egress. Reusing the request ID reads
its receipt; a timeout or interrupted dispatch is never replayed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.request import Request
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.inference.contracts import TransportError
from onefoundry.mail_api_sources import GRAPH

if TYPE_CHECKING:
    from onefoundry.connector_schemas import SignOutEverywhere
    from onefoundry.connector_store import ConnectorStore


def _receipt(
    connection: Connection, project_id: UUID, account_id: UUID, request_id: UUID
) -> dict[str, Any] | None:
    saved = (
        connection.execute(
            text(
                "SELECT id,account_id,state,error_code,created_at,finished_at "
                "FROM connector_signouts WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": request_id},
        )
        .mappings()
        .one_or_none()
    )
    if saved is None:
        return None
    if saved["account_id"] != account_id:
        raise Conflict("this sign-out request belongs to another account")
    return dict(saved)


def sign_out(
    store: ConnectorStore, project_id: UUID, account_id: UUID, data: SignOutEverywhere
) -> dict[str, Any]:
    if store.gateway.actor_id != store.settings.operator_actor_id:
        raise Conflict("only the operator may approve signing out everywhere")
    with store.gateway.engine.connect() as connection:
        prior = _receipt(connection, project_id, account_id, data.request_id)
        if prior is not None:
            return prior
        row = store._row(connection, project_id, account_id)
        # The claim transaction disables the account and inserts its receipt
        # atomically. A duplicate can observe that commit between these reads.
        prior = _receipt(connection, project_id, account_id, data.request_id)
        if prior is not None:
            return prior
    if row["version"] != data.expected_version or row["status"] != "connected":
        raise Conflict("the account changed; reload before signing out")
    if row["provider"] != "microsoft" or row["tenant"] == "consumers":
        raise Conflict("sign out everywhere requires a Microsoft work or school account")
    scopes = {scope.rsplit("/", 1)[-1].lower() for scope in row["granted_scopes"].split()}
    if "sign_out_everywhere" not in row["features"] or "user.revokesessions.all" not in scopes:
        raise Conflict("connect this account with the explicit Sign out everywhere permission first")
    try:
        token = store.access_token(project_id, account_id)
    except Unavailable:
        with store.gateway.engine.connect() as connection:
            saved = _receipt(connection, project_id, account_id, data.request_id)
        if saved is not None:
            return saved
        raise
    with store.gateway.engine.begin() as connection:
        current = store._row(connection, project_id, account_id, lock=True)
        prior = _receipt(connection, project_id, account_id, data.request_id)
        if prior is not None:
            return prior
        if current["version"] != data.expected_version or current["status"] != "connected":
            raise Conflict("the account changed; reload before signing out")
        connection.execute(
            text(
                "INSERT INTO connector_signouts (id,project_id,account_id,actor_id,state) "
                "VALUES (:id,:p,:a,:actor,'dispatching')"
            ),
            {"id": data.request_id, "p": project_id, "a": account_id, "actor": store.gateway.actor_id},
        )
        connection.execute(
            text(
                "UPDATE connector_accounts SET status='failed',last_error_code='signout_pending',"
                "version=version+1 WHERE project_id=:p AND id=:a"
            ),
            {"p": project_id, "a": account_id},
        )
        store.gateway._event(
            connection,
            project_id,
            None,
            None,
            "connector_signout_approved",
            {
                "request_id": str(data.request_id),
                "account_id": str(account_id),
                "scope": "all_apps_and_browsers",
            },
        )
    request = Request(  # noqa: S310 - fixed Microsoft Graph origin
        f"{GRAPH}/me/revokeSignInSessions",
        method="POST",
        data=b"",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    state, error = "unknown", "signout_outcome_unknown"
    try:
        response = store.http(request, timeout=float(store.settings.connector_http_timeout_seconds))
        if 400 <= response.status < 500:
            state, error = "failed", "signout_rejected"
        elif response.status == 200:
            try:
                confirmed = json.loads(response.body)
            except (ValueError, UnicodeDecodeError):
                confirmed = None
            if isinstance(confirmed, dict) and confirmed.get("value") is True:
                state, error = "completed", "signed_out_everywhere"
    except TransportError:
        pass  # The durable receipt remains uncertain; no retry.
    with store.gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_signouts SET state=:state,error_code=:error,finished_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id AND state='dispatching'"
            ),
            {
                "state": state,
                "error": None if state == "completed" else error,
                "p": project_id,
                "id": data.request_id,
            },
        )
        connection.execute(
            text(
                "UPDATE connector_accounts SET last_error_code=:e WHERE project_id=:p AND id=:a "
                "AND last_error_code='signout_pending'"
            ),
            {"p": project_id, "a": account_id, "e": error},
        )
        store.gateway._event(
            connection,
            project_id,
            None,
            None,
            f"connector_signout_{state}",
            {
                "request_id": str(data.request_id),
                "account_id": str(account_id),
                "error_code": None if state == "completed" else error,
            },
        )
        result = (
            connection.execute(
                text(
                    "SELECT id,account_id,state,error_code,created_at,finished_at "
                    "FROM connector_signouts WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": data.request_id},
            )
            .mappings()
            .one_or_none()
        )
        if result is None:
            raise NotFound("sign-out receipt not found")
        return dict(result)
