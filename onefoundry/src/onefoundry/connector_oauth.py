"""OAuth for operator-registered Microsoft and Google apps, against fixed official endpoints.

Microsoft uses the device-code flow (public client). Google uses the installed-app
authorization-code flow with PKCE and a loopback redirect to the local app. Token
bodies are returned to the caller for the protected secret store and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode
from urllib.request import Request

from onefoundry.inference.contracts import TransportError
from onefoundry.inference.http import HTTPTransport, send_http

Provider = Literal["microsoft", "google"]
Feature = Literal["mail", "files", "files_write", "sign_out_everywhere"]

MICROSOFT_AUTHORITY = "https://login.microsoftonline.com"
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint URL
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
BASE_SCOPES: dict[str, tuple[str, ...]] = {
    "microsoft": ("offline_access", "User.Read"),
    "google": ("openid", "email"),
}
FEATURE_SCOPES: dict[str, dict[str, tuple[str, ...]]] = {
    "microsoft": {
        "mail": ("Mail.Read",),
        "files": ("Files.Read.All",),
        "files_write": ("Files.ReadWrite.All",),
        "sign_out_everywhere": ("User.RevokeSessions.All",),
    },
    "google": {
        "mail": ("https://www.googleapis.com/auth/gmail.readonly",),
        "files": ("https://www.googleapis.com/auth/drive.readonly",),
        "files_write": ("https://www.googleapis.com/auth/drive.file",),
    },
}


class OAuthError(Exception):
    """A sign-in step failed with a fixed, secret-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: int


def scopes(provider: Provider, features: list[str]) -> list[str]:
    granted = list(BASE_SCOPES[provider])
    for feature in features:
        for scope in FEATURE_SCOPES[provider].get(feature, ()):
            if scope not in granted:
                granted.append(scope)
    return granted


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise OAuthError("invalid_response") from None
    if not isinstance(value, dict):
        raise OAuthError("invalid_response")
    return value


def _form(url: str, body: dict[str, str], http: HTTPTransport, timeout: float) -> tuple[int, dict[str, Any]]:
    request = Request(  # noqa: S310 - fixed official identity endpoints only
        url,
        data=urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        result = http(request, timeout=timeout)
    except TransportError:
        raise OAuthError("transport_failed") from None
    return result.status, _json(result.body) if result.body else {}


def _tenant(tenant: str) -> str:
    if not tenant or any(not (char.isalnum() or char in "-.") for char in tenant) or len(tenant) > 100:
        raise OAuthError("invalid_tenant")
    return tenant


def _tokens(data: dict[str, Any], prior_refresh: str | None = None) -> dict[str, Any]:
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError("invalid_response")
    expires_in = data.get("expires_in", 3600)
    refresh = data.get("refresh_token") or prior_refresh
    return {
        "access_token": access,
        "refresh_token": refresh if isinstance(refresh, str) else None,
        "expires_at": int(time.time()) + (int(expires_in) if str(expires_in).isdigit() else 3600),
        "scope": str(data.get("scope", "")),
    }


def microsoft_start(
    client_id: str, tenant: str, scope: list[str], *, http: HTTPTransport = send_http, timeout: float = 10
) -> DeviceStart:
    status, data = _form(
        f"{MICROSOFT_AUTHORITY}/{_tenant(tenant)}/oauth2/v2.0/devicecode",
        {"client_id": client_id, "scope": " ".join(scope)},
        http,
        timeout,
    )
    if status != 200:
        raise OAuthError("device_start_failed")
    device, user, uri = data.get("device_code"), data.get("user_code"), data.get("verification_uri")
    interval, expires = data.get("interval", 5), data.get("expires_in", 900)
    if not all(isinstance(item, str) and item for item in (device, user, uri)):
        raise OAuthError("invalid_response")
    if str(uri) != "https://microsoft.com/devicelogin" and not str(uri).startswith("https://login.microsoft"):
        raise OAuthError("invalid_response")
    return DeviceStart(
        str(device), str(user), str(uri), max(1, min(int(interval), 30)), int(time.time()) + int(expires)
    )


def microsoft_poll(
    client_id: str, tenant: str, device_code: str, *, http: HTTPTransport = send_http, timeout: float = 10
) -> dict[str, Any] | None:
    """Return tokens once approved, None while pending; raise on denial or expiry."""
    status, data = _form(
        f"{MICROSOFT_AUTHORITY}/{_tenant(tenant)}/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
        http,
        timeout,
    )
    if status == 200:
        return _tokens(data)
    error = str(data.get("error", ""))
    if error in {"authorization_pending", "slow_down"}:
        return None
    raise OAuthError(
        {"authorization_declined": "sign_in_declined", "expired_token": "sign_in_expired"}.get(
            error, "sign_in_failed"
        )
    )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def google_authorize_url(
    client_id: str, redirect_uri: str, scope: list[str], state: str, challenge: str
) -> str:
    return (
        GOOGLE_AUTHORIZE
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scope),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
    )


def google_exchange(
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    http: HTTPTransport = send_http,
    timeout: float = 10,
) -> dict[str, Any]:
    status, data = _form(
        GOOGLE_TOKEN,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        http,
        timeout,
    )
    if status != 200:
        raise OAuthError("code_exchange_failed")
    tokens = _tokens(data)
    if not tokens["refresh_token"]:
        raise OAuthError("no_refresh_token")
    return tokens


def refresh(
    provider: Provider,
    client_id: str,
    tenant: str,
    client_secret: str | None,
    refresh_token: str,
    *,
    http: HTTPTransport = send_http,
    timeout: float = 10,
) -> dict[str, Any]:
    if provider == "microsoft":
        url = f"{MICROSOFT_AUTHORITY}/{_tenant(tenant)}/oauth2/v2.0/token"
        body = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
    else:
        url = GOOGLE_TOKEN
        body = {
            "client_id": client_id,
            "client_secret": client_secret or "",
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    status, data = _form(url, body, http, timeout)
    if status in {400, 401}:
        raise OAuthError("refresh_rejected")
    if status != 200:
        raise OAuthError("refresh_failed")
    return _tokens(data, refresh_token)


def google_revoke(token: str, *, http: HTTPTransport = send_http, timeout: float = 10) -> None:
    try:
        _form(GOOGLE_REVOKE, {"token": token}, http, timeout)
    except OAuthError:
        pass  # Local credentials are removed regardless; revocation is best effort.
