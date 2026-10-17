"""Codex account connection adapted from DistrictAI and JintellarCore.

Device codes and tokens stay in OneFoundry's protected model-secret volume. The
browser receives only the verification URL and short user code.
"""

import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request

from onefoundry.inference import TransportError
from onefoundry.inference.http import HTTPTransport, send_http
from onefoundry.model_providers import DiscoveredModel, DiscoveryError
from onefoundry.settings import Settings


class CodexModelDiscoveryError(DiscoveryError):
    """A model-catalog failure containing only locally constructed, redacted detail."""


class CodexConnectionError(Exception):
    """A redacted account connection failure."""


class CodexConnectionPending(CodexConnectionError):
    """The operator has not finished the device verification."""


class CodexDeviceStartError(CodexConnectionError):
    """An upstream HTTP status stopped device login before local persistence."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"OpenAI sign-in endpoint returned HTTP {status}")


class CodexTokenExchangeError(CodexConnectionError):
    """The OAuth token endpoint rejected a verified device authorization."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"OpenAI token exchange returned HTTP {status}")


class CodexTokenExchangeUnknown(CodexConnectionError):
    """The one-time token request may have reached OpenAI before transport failed."""


@dataclass(frozen=True)
class DeviceChallenge:
    verification_url: str
    user_code: str
    expires_at: int
    interval_seconds: int
    device_code: str


AUTH_ORIGIN = "https://auth.openai.com"
API_ORIGIN = "https://chatgpt.com/backend-api"
VERIFY_URL = AUTH_ORIGIN + "/codex/device"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ACCOUNT_CLAIM = "https://api.openai.com/auth"
USER_AGENT = "OneFoundry/0.1"


def _json_response(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise CodexConnectionError("Codex connection returned invalid JSON") from None
    if not isinstance(value, dict):
        raise CodexConnectionError("Codex connection returned invalid data")
    return value


def _post(path: str, body: dict[str, str], timeout: int, http: HTTPTransport) -> tuple[int, dict[str, Any]]:
    request = Request(  # noqa: S310 - exact fixed OpenAI authentication origin
        AUTH_ORIGIN + path,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    result = http(request, timeout=timeout)
    return result.status, _json_response(result.body) if result.status == 200 and result.body else {}


def _token_exchange(body: dict[str, str], timeout: int, http: HTTPTransport) -> dict[str, Any]:
    request = Request(  # noqa: S310 - exact fixed OpenAI authentication origin
        AUTH_ORIGIN + "/oauth/token",
        data=urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        result = http(request, timeout=timeout)
    except TransportError:
        raise CodexTokenExchangeUnknown("Codex token exchange outcome is unknown") from None
    if result.status != 200:
        raise CodexTokenExchangeError(result.status)
    return _json_response(result.body)


def start_device_login(settings: Settings, http: HTTPTransport = send_http) -> DeviceChallenge:
    status, data = _post(
        "/api/accounts/deviceauth/usercode",
        {"client_id": CLIENT_ID},
        settings.model_discovery_timeout_seconds,
        http,
    )
    if status != 200:
        raise CodexDeviceStartError(status)
    device_code = data.get("device_auth_id")
    user_code = data.get("user_code") or data.get("usercode")
    interval = data.get("interval", 5)
    if not isinstance(device_code, str) or not device_code or not isinstance(user_code, str) or not user_code:
        raise CodexConnectionError("Codex device login could not start")
    if isinstance(interval, str) and interval.isdecimal():
        interval = int(interval)
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 30:
        raise CodexConnectionError("Codex device login returned an invalid interval")
    return DeviceChallenge(
        VERIFY_URL, user_code, int(time.time()) + settings.codex_device_timeout_seconds, interval, device_code
    )


def _token_claims(access_token: str) -> dict[str, Any]:
    try:
        segment = access_token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload if isinstance(payload, dict) else {}
    except (AttributeError, IndexError, ValueError, UnicodeDecodeError, binascii.Error):
        return {}


def _account_id(access_token: str) -> str:
    claim = _token_claims(access_token).get(ACCOUNT_CLAIM)
    value = claim.get("chatgpt_account_id") if isinstance(claim, dict) else None
    return value if isinstance(value, str) and value else ""


def _access_expiry(data: dict[str, Any], access_token: str) -> int | None:
    lifetime = data.get("expires_in")
    if isinstance(lifetime, str) and lifetime.isdecimal():
        lifetime = int(lifetime)
    if lifetime is not None:
        if not isinstance(lifetime, int) or isinstance(lifetime, bool) or lifetime <= 0:
            raise CodexConnectionError("Codex login returned an invalid expiry")
        return int(time.time()) + lifetime
    exp = _token_claims(access_token).get("exp")
    return exp if isinstance(exp, int) and not isinstance(exp, bool) and exp > 0 else None


def _credential(data: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    access = data.get("access_token")
    refresh = data.get("refresh_token") or (old or {}).get("refresh_token")
    account_id = data.get("account_id") or data.get("organization_id")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise CodexConnectionError("Codex login did not return reusable credentials")
    if not isinstance(account_id, str) or not account_id:
        account_id = _account_id(access)
        id_token = data.get("id_token")
        if not account_id and isinstance(id_token, str):
            account_id = _account_id(id_token)
        account_id = account_id or (old or {}).get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise CodexConnectionError("Codex login did not identify the account")
    return {
        "kind": "connected",
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account_id,
        "expires_at": _access_expiry(data, access),
    }


def finish_device_login(
    pending: dict[str, Any],
    settings: Settings,
    http: HTTPTransport = send_http,
    *,
    on_exchange: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if pending.get("kind") == "exchanged":
        tokens = pending.get("tokens")
        if not isinstance(tokens, dict):
            raise CodexConnectionError("Codex saved token response is invalid")
        return _credential(tokens)
    if (
        pending.get("kind") != "pending"
        or not isinstance(pending.get("device_code"), str)
        or not isinstance(pending.get("user_code"), str)
    ):
        raise CodexConnectionError("Codex connection is not pending")
    if int(time.time()) >= pending.get("expires_at", 0):
        raise CodexConnectionError("Codex device login expired; start again")
    status, data = _post(
        "/api/accounts/deviceauth/token",
        {"device_auth_id": pending["device_code"], "user_code": pending["user_code"]},
        settings.model_discovery_timeout_seconds,
        http,
    )
    if status in {403, 404, 428}:
        raise CodexConnectionPending("Complete Codex sign-in, then check again")
    code = data.get("authorization_code")
    verifier = data.get("code_verifier")
    if (
        status != 200
        or not isinstance(code, str)
        or not code
        or not isinstance(verifier, str)
        or not verifier
    ):
        raise CodexConnectionError("Codex device login failed")
    tokens = _token_exchange(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": AUTH_ORIGIN + "/deviceauth/callback",
        },
        settings.codex_auth_exchange_timeout_seconds,
        http,
    )
    if on_exchange is not None:
        on_exchange({"kind": "exchanged", "tokens": tokens})
    return _credential(tokens)


def refresh_credential(
    credential: dict[str, Any], settings: Settings, http: HTTPTransport = send_http
) -> dict[str, Any]:
    refresh = credential.get("refresh_token")
    if credential.get("kind") != "connected" or not isinstance(refresh, str) or not refresh:
        raise CodexConnectionError("Codex account needs reconnection")
    return _credential(
        _token_exchange(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh,
            },
            settings.codex_auth_exchange_timeout_seconds,
            http,
        ),
        credential,
    )


def encode_state(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_state(value: str) -> dict[str, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(value))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise CodexConnectionError("Codex account credential is invalid") from None
    if not isinstance(data, dict) or data.get("kind") not in {"pending", "exchanged", "connected"}:
        raise CodexConnectionError("Codex account credential is invalid")
    return data


def discover_codex_models(
    credential: dict[str, Any], settings: Settings, http: HTTPTransport = send_http
) -> list[DiscoveredModel]:
    if credential.get("kind") != "connected":
        raise CodexModelDiscoveryError("Codex account is not connected")
    request = Request(  # noqa: S310 - exact fixed OpenAI account endpoint
        API_ORIGIN + "/codex/models?" + urlencode({"client_version": settings.codex_catalog_client_version}),
        headers={
            "Authorization": "Bearer " + credential["access_token"],
            "chatgpt-account-id": credential["account_id"],
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        response = http(request, timeout=settings.model_discovery_timeout_seconds)
    except TransportError:
        raise CodexModelDiscoveryError(
            "Codex model discovery transport failed; check connectivity."
        ) from None
    if response.status != 200:
        action = "check account authorization." if response.status in {401, 403} else "retry discovery later."
        raise CodexModelDiscoveryError(f"Codex model discovery returned HTTP {response.status}; {action}")
    try:
        body = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise CodexModelDiscoveryError("Codex model catalog is invalid") from None
    rows = body.get("models") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) > settings.model_discovery_max_models:
        raise CodexModelDiscoveryError("Codex model catalog is invalid")
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CodexModelDiscoveryError("Codex model catalog is invalid")
        if row.get("visibility", "list") != "list":
            continue
        slug = row.get("slug")
        if (
            not isinstance(slug, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", slug)
            or slug in seen
        ):
            raise CodexModelDiscoveryError("Codex model catalog has an invalid identifier")
        seen.add(slug)
        display = row.get("display_name")
        name = display.strip() if isinstance(display, str) and display.strip() else slug
        raw_levels = row.get("supported_reasoning_levels", [])
        if not isinstance(raw_levels, list) or len(raw_levels) > 16:
            raise CodexModelDiscoveryError("Codex model catalog has invalid reasoning levels")
        levels: list[str] = []
        for item in raw_levels:
            effort = item.get("effort") if isinstance(item, dict) else None
            if not isinstance(effort, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", effort):
                raise CodexModelDiscoveryError("Codex model catalog has invalid reasoning levels")
            if effort not in levels:
                levels.append(effort)
        models.append(DiscoveredModel(slug, name[:200], "text", tuple(levels)))
    return models
