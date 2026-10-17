"""Text-only Tencent iLink wire transport, independent of the OpenClaw runtime.

The caller owns pairing confirmation, credential storage, cursor commits, inbound
authorization, and outbound idempotency. This module performs no persistence.
Protocol reference: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

_FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"


class WeixinProtocolError(Exception):
    """A rejected input, upstream response, or dependency failure without secret details."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class WeixinSendUnknown(WeixinProtocolError):
    """The send request may have reached the backend; never retry automatically."""

    def __init__(self) -> None:
        super().__init__("send_outcome_unknown")


class QRStatus(StrEnum):
    WAIT = "wait"
    SCANED = "scaned"
    EXPIRED = "expired"
    NEED_VERIFYCODE = "need_verifycode"
    VERIFY_CODE_BLOCKED = "verify_code_blocked"
    SCANED_BUT_REDIRECT = "scaned_but_redirect"
    BINDED_REDIRECT = "binded_redirect"
    CONFIRMED = "confirmed"
    CONFIRMED_UNBOUND = "confirmed_unbound"


@dataclass(frozen=True)
class QRChallenge:
    qrcode: str = field(repr=False)
    image_content: str = field(repr=False)
    poll_base_url: str


@dataclass(frozen=True)
class WeixinCredentials:
    bot_id: str
    scanner_user_id: str
    token: str = field(repr=False)
    base_url: str


@dataclass(frozen=True)
class QRPollResult:
    status: QRStatus
    poll_base_url: str
    credentials: WeixinCredentials | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InboundText:
    message_id: str
    sender_id: str
    recipient_id: str
    text: str = field(repr=False)
    context_token: str = field(repr=False)
    group_id: str | None = None

    @property
    def is_dm(self) -> bool:
        return self.group_id is None


@dataclass(frozen=True)
class UpdateBatch:
    messages: tuple[InboundText, ...] = field(repr=False)
    cursor: str = field(repr=False)
    next_timeout_seconds: int = 35


@dataclass(frozen=True)
class SendAcceptance:
    status: Literal["accepted_by_platform"]
    client_id: str


def _identifier(value: object, code: str, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise WeixinProtocolError(code)
    return value


def _base_url(value: str) -> str:
    """Accept the fixed API host and Tencent iLink IDC hosts only."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise WeixinProtocolError("invalid_ilink_host") from None
    if (
        parsed.scheme != "https"
        or host is None
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc != host
    ):
        raise WeixinProtocolError("invalid_ilink_host")
    if host != "ilinkai.weixin.qq.com":
        suffix = ".weixin.qq.com"
        prefix = host[: -len(suffix)] if host.endswith(suffix) else ""
        if not prefix.startswith("ilinkai-") or not prefix[8:] or len(prefix) > 48:
            raise WeixinProtocolError("invalid_ilink_host")
        if not all(c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in prefix):
            raise WeixinProtocolError("invalid_ilink_host")
        if prefix.endswith("-"):
            raise WeixinProtocolError("invalid_ilink_host")
    return f"https://{host}"


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise WeixinProtocolError("invalid_ilink_response") from None
    if not isinstance(parsed, dict):
        raise WeixinProtocolError("invalid_ilink_response")
    return parsed


def _success_code(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


@dataclass(frozen=True)
class WeixinILinkConfig:
    """Validated protocol snapshot and bounded transport policy supplied by Settings."""

    api_base_url: str = _FIXED_BASE_URL
    channel_version: str = "2.4.9"
    app_client_version: int = 132105  # 0x00020409 for Tencent openclaw-weixin 2.4.9.
    bot_agent: str = "OneFoundry/0.1.0"
    qr_request_timeout_seconds: float = 5
    qr_poll_timeout_seconds: float = 35
    poll_timeout_seconds: int = 35
    max_poll_timeout_seconds: int = 40
    send_timeout_seconds: float = 15
    update_retries: int = 3
    retry_backoff_seconds: float = 0.5
    max_response_bytes: int = 1_048_576
    max_cursor_length: int = 2048
    max_text_length: int = 8192

    def __post_init__(self) -> None:
        if _base_url(self.api_base_url) != _FIXED_BASE_URL:
            raise WeixinProtocolError("invalid_ilink_config")
        if not isinstance(self.channel_version, str) or not re.fullmatch(
            r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", self.channel_version
        ):
            raise WeixinProtocolError("invalid_ilink_config")
        if (
            not isinstance(self.app_client_version, int)
            or isinstance(self.app_client_version, bool)
            or not 1 <= self.app_client_version <= 0xFFFFFF
        ):
            raise WeixinProtocolError("invalid_ilink_config")
        if (
            not isinstance(self.bot_agent, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", self.bot_agent)
            or len(self.bot_agent) > 256
        ):
            raise WeixinProtocolError("invalid_ilink_config")
        numeric_bounds = (
            (self.qr_request_timeout_seconds, 1, 60),
            (self.qr_poll_timeout_seconds, 1, 60),
            (self.send_timeout_seconds, 1, 60),
            (self.retry_backoff_seconds, 0.01, 10),
        )
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
            for value, low, high in numeric_bounds
        ):
            raise WeixinProtocolError("invalid_ilink_config")
        integer_bounds = (
            (self.poll_timeout_seconds, 1, 40),
            (self.max_poll_timeout_seconds, 1, 60),
            (self.update_retries, 1, 5),
            (self.max_response_bytes, 1024, 8_388_608),
            (self.max_cursor_length, 1, 8192),
            (self.max_text_length, 1, 8192),
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high
            for value, low, high in integer_bounds
        ):
            raise WeixinProtocolError("invalid_ilink_config")
        if self.poll_timeout_seconds > self.max_poll_timeout_seconds:
            raise WeixinProtocolError("invalid_ilink_config")

    def base_info(self) -> dict[str, str]:
        return {"channel_version": self.channel_version, "bot_agent": self.bot_agent}


class WeixinILinkClient:
    """Bounded transport with no ambient proxy, cookies, redirects, or live startup."""

    def __init__(
        self,
        *,
        config: WeixinILinkConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        self._sleep = backoff_sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, token: str | None = None, *, qr_get: bool = False) -> dict[str, str]:
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(self._config.app_client_version)}
        if not qr_get:
            uin = str(secrets.randbits(32)).encode("ascii")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(uin).decode("ascii"),
                }
            )
        if token is not None:
            headers["Authorization"] = f"Bearer {_identifier(token, 'invalid_ilink_token', limit=4096)}"
        return headers

    async def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        token: str | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        url = _base_url(base_url) + path
        try:
            async with self._client.stream(
                method,
                url,
                params=params,
                json=body,
                headers=self._headers(token, qr_get=method == "GET"),
                timeout=httpx.Timeout(timeout),
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise WeixinProtocolError("ilink_http_error", retryable=response.status_code >= 500)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._config.max_response_bytes:
                        raise WeixinProtocolError("ilink_response_too_large")
                    chunks.append(chunk)
        except httpx.RequestError:
            raise WeixinProtocolError("ilink_transport_unavailable", retryable=True) from None
        return _json_object(b"".join(chunks))

    async def start_qr(self) -> QRChallenge:
        payload = await self._request(
            "POST",
            self._config.api_base_url,
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            body={"local_token_list": []},
            timeout=self._config.qr_request_timeout_seconds,
        )
        qrcode = _identifier(payload.get("qrcode"), "invalid_qr_response", limit=4096)
        image = payload.get("qrcode_img_content")
        if not isinstance(image, str) or not image or len(image) > 65_536:
            raise WeixinProtocolError("invalid_qr_response")
        return QRChallenge(qrcode, image, self._config.api_base_url)

    async def poll_qr(
        self,
        challenge: QRChallenge,
        *,
        verify_code: str | None = None,
        expected_scanner_user_id: str | None = None,
        expected_bot_id: str | None = None,
    ) -> QRPollResult:
        if (expected_scanner_user_id is None) != (expected_bot_id is None):
            raise WeixinProtocolError("incomplete_qr_binding")
        if verify_code is not None:
            _identifier(verify_code, "invalid_verify_code", limit=32)
        base_url = _base_url(challenge.poll_base_url)
        params = {"qrcode": _identifier(challenge.qrcode, "invalid_qr_code", limit=4096)}
        if verify_code is not None:
            params["verify_code"] = verify_code
        payload = await self._request(
            "GET",
            base_url,
            "/ilink/bot/get_qrcode_status",
            params=params,
            timeout=self._config.qr_poll_timeout_seconds,
        )
        raw_status = payload.get("status")
        if not isinstance(raw_status, str):
            raise WeixinProtocolError("invalid_qr_status")
        try:
            status = QRStatus(raw_status)
        except ValueError:
            raise WeixinProtocolError("invalid_qr_status") from None
        if status is QRStatus.SCANED_BUT_REDIRECT:
            redirect = payload.get("redirect_host")
            if not isinstance(redirect, str):
                raise WeixinProtocolError("invalid_ilink_host")
            return QRPollResult(status, _base_url(f"https://{redirect}"))
        if status is not QRStatus.CONFIRMED:
            return QRPollResult(status, base_url)
        bot_id = _identifier(payload.get("ilink_bot_id"), "invalid_qr_confirmation")
        scanner_id = _identifier(payload.get("ilink_user_id"), "invalid_qr_confirmation")
        token = _identifier(payload.get("bot_token"), "invalid_qr_confirmation", limit=4096)
        confirmed_base_url = _base_url(payload.get("baseurl", base_url))
        if expected_scanner_user_id is not None:
            if scanner_id != expected_scanner_user_id or bot_id != expected_bot_id:
                raise WeixinProtocolError("qr_identity_mismatch")
            return QRPollResult(
                status, confirmed_base_url, WeixinCredentials(bot_id, scanner_id, token, confirmed_base_url)
            )
        return QRPollResult(
            QRStatus.CONFIRMED_UNBOUND,
            confirmed_base_url,
            WeixinCredentials(bot_id, scanner_id, token, confirmed_base_url),
        )

    async def get_updates(
        self, credentials: WeixinCredentials, cursor: str, *, timeout_seconds: int | None = None
    ) -> UpdateBatch:
        if not isinstance(cursor, str) or len(cursor) > self._config.max_cursor_length:
            raise WeixinProtocolError("invalid_ilink_cursor")
        if timeout_seconds is None:
            timeout_seconds = self._config.poll_timeout_seconds
        if not 1 <= timeout_seconds <= self._config.max_poll_timeout_seconds:
            raise WeixinProtocolError("invalid_ilink_timeout")
        body = {
            "get_updates_buf": cursor,
            "base_info": self._config.base_info(),
        }
        for attempt in range(self._config.update_retries):
            try:
                payload = await self._request(
                    "POST",
                    credentials.base_url,
                    "/ilink/bot/getupdates",
                    body=body,
                    token=credentials.token,
                    timeout=timeout_seconds,
                )
            except WeixinProtocolError as error:
                if not error.retryable or attempt == self._config.update_retries - 1:
                    raise
                await self._sleep(min(10.0, self._config.retry_backoff_seconds * (2**attempt)))
                continue
            if not _success_code(payload.get("ret")) or not _success_code(payload.get("errcode", 0)):
                if payload.get("ret") == -14 or payload.get("errcode") == -14:
                    raise WeixinProtocolError("ilink_session_paused")
                raise WeixinProtocolError("ilink_updates_rejected")
            next_cursor = payload.get("get_updates_buf") or cursor
            if not isinstance(next_cursor, str) or len(next_cursor) > self._config.max_cursor_length:
                raise WeixinProtocolError("invalid_ilink_cursor")
            raw_messages = payload.get("msgs", [])
            if not isinstance(raw_messages, list):
                raise WeixinProtocolError("invalid_ilink_response")
            messages = tuple(message for raw in raw_messages if (message := _text_message(raw)) is not None)
            suggested = payload.get("longpolling_timeout_ms", 35_000)
            if not isinstance(suggested, int) or isinstance(suggested, bool) or suggested <= 0:
                suggested = 35_000
            return UpdateBatch(
                messages, next_cursor, max(1, min(self._config.max_poll_timeout_seconds, suggested // 1000))
            )
        raise WeixinProtocolError("ilink_transport_unavailable", retryable=True)

    async def send_text(
        self,
        credentials: WeixinCredentials,
        recipient_id: str,
        context_token: str,
        client_id: str,
        text: str,
    ) -> SendAcceptance:
        recipient = _identifier(recipient_id, "invalid_ilink_recipient")
        if (
            not recipient.endswith("@im.wechat")
            or not recipient[: -len("@im.wechat")]
            or "@" in recipient[: -len("@im.wechat")]
        ):
            raise WeixinProtocolError("invalid_ilink_recipient")
        context = _identifier(context_token, "invalid_ilink_context", limit=4096)
        stable_id = _identifier(client_id, "invalid_ilink_client_id", limit=128)
        _identifier(credentials.token, "invalid_ilink_token", limit=4096)
        _base_url(credentials.base_url)
        if not isinstance(text, str) or not text.strip() or len(text) > self._config.max_text_length:
            raise WeixinProtocolError("invalid_ilink_text")
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": recipient,
                "client_id": stable_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": self._config.base_info(),
        }
        try:
            response = await self._request(
                "POST",
                credentials.base_url,
                "/ilink/bot/sendmessage",
                body=body,
                token=credentials.token,
                timeout=self._config.send_timeout_seconds,
            )
        except WeixinProtocolError:
            raise WeixinSendUnknown() from None
        if not _success_code(response.get("ret")):
            if isinstance(response.get("ret"), int) and not isinstance(response.get("ret"), bool):
                raise WeixinProtocolError("ilink_send_rejected")
            raise WeixinSendUnknown()
        return SendAcceptance("accepted_by_platform", stable_id)


def _text_message(raw: Any) -> InboundText | None:
    if not isinstance(raw, dict):
        raise WeixinProtocolError("invalid_ilink_message")
    if raw.get("message_type") == 2 or raw.get("group_id") not in (None, ""):
        return None
    items = raw.get("item_list", [])
    if not isinstance(items, list):
        raise WeixinProtocolError("invalid_ilink_message")
    texts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise WeixinProtocolError("invalid_ilink_message")
        if item.get("type") == 1:
            text_item = item.get("text_item")
            if not isinstance(text_item, dict) or not isinstance(text_item.get("text"), str):
                raise WeixinProtocolError("invalid_ilink_message")
            if text_item["text"]:
                texts.append(text_item["text"])
    if not texts:
        return None
    message_id = raw.get("message_id")
    if (
        isinstance(message_id, bool)
        or not isinstance(message_id, (int, str))
        or not str(message_id).isdigit()
    ):
        raise WeixinProtocolError("invalid_ilink_message")
    group_id = raw.get("group_id")
    if group_id is not None:
        group_id = _identifier(group_id, "invalid_ilink_message")
    return InboundText(
        str(message_id),
        _identifier(raw.get("from_user_id"), "invalid_ilink_message"),
        _identifier(raw.get("to_user_id"), "invalid_ilink_message"),
        "\n".join(texts),
        _identifier(raw.get("context_token"), "invalid_ilink_message", limit=4096),
        group_id,
    )
