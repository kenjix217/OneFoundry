"""Grok subscription account OAuth and catalog access.

Callers persist the returned state in OneFoundry's protected secret store. Only
the verification URL and short user code belong in a browser response.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from onefoundry.inference.http import HTTPResult, HTTPTransport, send_http
from onefoundry.model_providers import DiscoveredModel, DiscoveryError
from onefoundry.model_secrets import MAX_CREDENTIAL_BYTES
from onefoundry.settings import Settings


class GrokConnectionError(Exception):
    """An account connection failure with no provider response or credential."""


class GrokConnectionPending(GrokConnectionError):
    """Device verification has not finished."""

    def __init__(
        self, message: str = "Complete Grok sign-in, then check again", *, interval_seconds: int | None = None
    ) -> None:
        super().__init__(message)
        self.interval_seconds = interval_seconds


class GrokConnectionExpired(GrokConnectionError):
    """The device challenge expired; a fresh challenge is required."""


class GrokConnectionDenied(GrokConnectionError):
    """The operator or provider denied this challenge."""


class GrokConnectionUnknown(GrokConnectionError):
    """The one-time token request may have completed; never replay it automatically."""


class GrokCredentialTooLarge(GrokConnectionError):
    """The provider returned credentials beyond the protected store's capacity."""


@dataclass(frozen=True)
class DeviceChallenge:
    verification_url: str
    user_code: str
    expires_at: int
    interval_seconds: int
    device_code: str = field(repr=False)


SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
# The Grok subscription transport rejects slash-bearing model IDs.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+\-]{0,199}\Z")
_MAX_OAUTH_RESPONSE_BYTES = 65_536


def valid_model_id(model_id: str) -> bool:
    """Catalog and inference share the same header-safe model identifiers."""
    return _MODEL_ID.fullmatch(model_id) is not None


def valid_account_token(value: object) -> bool:
    """Only bounded printable ASCII is safe in credential headers and form grants."""
    return isinstance(value, str) and 1 <= len(value) <= 16_384 and all(33 <= ord(c) <= 126 for c in value)


def _client_id(settings: Settings) -> str:
    value = settings.xai_oauth_client_id.strip()
    if not value:
        raise GrokConnectionError("xAI OAuth client is not configured")
    if _CLIENT_ID.fullmatch(value) is None:
        raise GrokConnectionError("xAI OAuth client ID is invalid")
    return value


def _trusted_xai_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise GrokConnectionError("xAI OAuth URL is missing or invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise GrokConnectionError("xAI OAuth URL is invalid") from None
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or host not in {"auth.x.ai", "accounts.x.ai"}
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise GrokConnectionError("xAI OAuth URL is untrusted")
    return value


def _json_object(result: HTTPResult, *, limit: int, error: str) -> dict[str, Any]:
    if len(result.body) > limit:
        raise GrokConnectionError(error)
    try:
        value = json.loads(result.body)
    except (ValueError, UnicodeDecodeError):
        raise GrokConnectionError(error) from None
    if not isinstance(value, dict):
        raise GrokConnectionError(error)
    return value


def _send(request: Request, timeout: int, http: HTTPTransport, error: str) -> HTTPResult:
    try:
        return http(request, timeout=timeout)
    except Exception:
        # Transport exceptions may include a URL, request body, or token.
        raise GrokConnectionError(error) from None


def _endpoints(settings: Settings, http: HTTPTransport) -> tuple[str, str]:
    response = _send(
        Request(  # noqa: S310 - exact issuer validated at settings boundary
            settings.xai_oauth_issuer + "/.well-known/openid-configuration",
            headers={"Accept": "application/json"},
        ),
        settings.model_discovery_timeout_seconds,
        http,
        "xAI OAuth discovery failed",
    )
    if response.status != 200:
        raise GrokConnectionError("xAI OAuth discovery failed")
    data = _json_object(response, limit=_MAX_OAUTH_RESPONSE_BYTES, error="xAI OAuth discovery is invalid")
    if data.get("issuer") != settings.xai_oauth_issuer:
        raise GrokConnectionError("xAI OAuth discovery issuer is invalid")
    device = _trusted_xai_url(data.get("device_authorization_endpoint"))
    token = _trusted_xai_url(data.get("token_endpoint"))
    if (
        device != settings.xai_oauth_issuer + "/oauth2/device/code"
        or token != settings.xai_oauth_issuer + "/oauth2/token"
    ):
        raise GrokConnectionError("xAI OAuth discovery endpoints are untrusted")
    return device, token


def _post_form(
    url: str, fields: dict[str, str], settings: Settings, http: HTTPTransport
) -> tuple[int, dict[str, Any]]:
    outbound = Request(  # noqa: S310 - URL is validated against the x.ai domain
        url,
        data=urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    response = _send(outbound, settings.model_discovery_timeout_seconds, http, "xAI OAuth request failed")
    return response.status, _json_object(
        response, limit=_MAX_OAUTH_RESPONSE_BYTES, error="xAI OAuth response is invalid"
    )


def _seconds(value: object, *, maximum: int, fallback: int = 0) -> int:
    if isinstance(value, str) and len(value) <= 10 and value.isdecimal():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, maximum)
    return fallback


def start_device_login(settings: Settings, http: HTTPTransport = send_http) -> DeviceChallenge:
    client_id = _client_id(settings)
    device_endpoint, _ = _endpoints(settings, http)
    status, data = _post_form(
        device_endpoint, {"client_id": client_id, "scope": SCOPE, "referrer": "onefoundry"}, settings, http
    )
    if status != 200:
        raise GrokConnectionError("Grok device login could not start")
    device_code = data.get("device_code")
    user_code = data.get("user_code")
    if (
        not isinstance(device_code, str)
        or not 1 <= len(device_code) <= 4096
        or not isinstance(user_code, str)
        or re.fullmatch(r"[A-Za-z0-9-]{1,128}", user_code) is None
    ):
        raise GrokConnectionError("Grok device login returned invalid codes")
    verification_url = _trusted_xai_url(data.get("verification_uri"))
    expiry = _seconds(data.get("expires_in"), maximum=900, fallback=300)
    interval = _seconds(data.get("interval", 5), maximum=86_400)
    if not interval:
        raise GrokConnectionError("Grok device login returned an invalid polling interval")
    return DeviceChallenge(verification_url, user_code, int(time.time()) + expiry, interval, device_code)


def _identity_hash(id_token: object) -> str:
    if not isinstance(id_token, str) or len(id_token) > 16_384:
        return ""
    try:
        segment = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        subject = claims.get("sub") if isinstance(claims, dict) else None
    except (IndexError, ValueError, UnicodeDecodeError, binascii.Error):
        return ""
    return hashlib.sha256(subject.encode()).hexdigest() if isinstance(subject, str) and subject else ""


def _credential(data: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    access = data.get("access_token")
    refresh = data.get("refresh_token") or (old or {}).get("refresh_token")
    expiry = _seconds(data.get("expires_in"), maximum=31 * 86_400)
    if (
        not valid_account_token(access)
        or not valid_account_token(refresh)
        or not expiry
        or str(data.get("token_type", "Bearer")).lower() != "bearer"
    ):
        raise GrokConnectionError("Grok login did not return renewable credentials")
    identity_hash = _identity_hash(data.get("id_token")) or (old or {}).get("account_id_hash", "")
    state = {
        "kind": "connected",
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": int(time.time()) + expiry,
        "account_id_hash": identity_hash,
    }
    encode_state(state)
    return state


def finish_device_login(
    pending: dict[str, Any], settings: Settings, http: HTTPTransport = send_http
) -> dict[str, Any]:
    client_id = _client_id(settings)
    device_code = pending.get("device_code")
    expires_at = pending.get("expires_at")
    if (
        pending.get("kind") != "pending"
        or not isinstance(device_code, str)
        or not 1 <= len(device_code) <= 4096
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
    ):
        raise GrokConnectionError("Grok connection is not pending")
    if int(time.time()) >= expires_at:
        raise GrokConnectionExpired("Grok device login expired; start fresh sign-in on this account")
    _, token_endpoint = _endpoints(settings, http)
    try:
        status, data = _post_form(
            token_endpoint,
            {"grant_type": DEVICE_GRANT, "client_id": client_id, "device_code": device_code},
            settings,
            http,
        )
    except GrokConnectionError:
        raise GrokConnectionUnknown(
            "Grok token exchange outcome is unknown; start fresh sign-in on this account"
        ) from None
    if status != 200:
        code = data.get("error")
        if code == "authorization_pending":
            raise GrokConnectionPending()
        if code == "slow_down":
            interval = _seconds(pending.get("interval_seconds"), maximum=86_400, fallback=5)
            raise GrokConnectionPending(interval_seconds=interval + 5)
        if code == "expired_token":
            raise GrokConnectionExpired("Grok device login expired; start fresh sign-in on this account")
        if code in {"access_denied", "authorization_denied"}:
            raise GrokConnectionDenied("Grok device login was denied; start fresh sign-in on this account")
        if status >= 500:
            raise GrokConnectionUnknown(
                "Grok token exchange outcome is unknown; start fresh sign-in on this account"
            )
        raise GrokConnectionError("Grok device login failed")
    try:
        return _credential(data)
    except GrokCredentialTooLarge:
        raise
    except GrokConnectionError:
        raise GrokConnectionUnknown(
            "Grok token response was invalid; start fresh sign-in on this account"
        ) from None


def refresh_credential(
    credential: dict[str, Any], settings: Settings, http: HTTPTransport = send_http
) -> dict[str, Any]:
    client_id = _client_id(settings)
    refresh = credential.get("refresh_token")
    if credential.get("kind") != "connected" or not valid_account_token(refresh):
        raise GrokConnectionError("Grok account needs reconnection")
    _, token_endpoint = _endpoints(settings, http)
    try:
        status, data = _post_form(
            token_endpoint,
            {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": str(refresh)},
            settings,
            http,
        )
    except GrokConnectionError:
        raise GrokConnectionUnknown("Grok token renewal outcome is unknown; sign in again") from None
    if status != 200:
        if status in {400, 401}:
            raise GrokConnectionError("Grok account needs reconnection")
        if status >= 500:
            raise GrokConnectionUnknown("Grok token renewal outcome is unknown; sign in again")
        raise GrokConnectionError("Grok token refresh failed")
    try:
        return _credential(data, credential)
    except GrokCredentialTooLarge:
        raise
    except GrokConnectionError:
        raise GrokConnectionUnknown("Grok token renewal response was invalid; sign in again") from None


def encode_state(state: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()
    if len(encoded) > MAX_CREDENTIAL_BYTES:
        raise GrokCredentialTooLarge(
            "Grok credential exceeds protected storage capacity; connection not saved"
        )
    return encoded


def decode_state(value: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(value))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise GrokConnectionError("Grok account credential is invalid") from None
    if not isinstance(data, dict) or data.get("kind") not in {
        "pending",
        "connected",
        "exchanging",
        "unknown",
        "failed",
        "refreshing",
    }:
        raise GrokConnectionError("Grok account credential is invalid")
    return data


def discover_grok_models(
    credential: dict[str, Any], settings: Settings, http: HTTPTransport = send_http
) -> list[DiscoveredModel]:
    token = credential.get("access_token")
    if credential.get("kind") != "connected" or not valid_account_token(token):
        raise DiscoveryError("Grok account is not connected")
    outbound = Request(  # noqa: S310 - exact fixed Grok subscription endpoint
        settings.grok_account_base_url + "/models",
        headers={
            "Authorization": "Bearer " + str(token),
            "X-XAI-Token-Auth": "xai-grok-cli",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        response = http(outbound, timeout=settings.model_discovery_timeout_seconds)
    except Exception:
        raise DiscoveryError("Grok model discovery failed") from None
    if response.status != 200:
        raise DiscoveryError("Grok model discovery failed; reconnect if the account expired")
    if len(response.body) > settings.model_discovery_max_response_bytes:
        raise DiscoveryError("Grok model catalog is too large")
    try:
        data = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise DiscoveryError("Grok model catalog is invalid") from None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > settings.model_discovery_max_models:
        raise DiscoveryError("Grok model catalog is invalid")
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id") or row.get("model")
        backend = row.get("api_backend") or row.get("apiBackend") or row.get("backend") or ""
        if not isinstance(backend, str) or backend.lower() not in {
            "",
            "responses",
            "chat",
            "chat_completions",
            "language",
        }:
            continue
        if not isinstance(model_id, str) or not valid_model_id(model_id):
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        display = row.get("name")
        name = display.strip() if isinstance(display, str) and display.strip() else model_id
        models.append(DiscoveredModel(model_id, name[:200], "text"))
    if not models:
        raise DiscoveryError("Grok model catalog has no usable models")
    return models
