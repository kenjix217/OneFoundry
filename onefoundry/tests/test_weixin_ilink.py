"""Offline wire-contract tests for the Tencent iLink text transport."""

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from onefoundry.weixin_ilink import (
    QRChallenge,
    QRStatus,
    WeixinCredentials,
    WeixinILinkClient,
    WeixinILinkConfig,
    WeixinProtocolError,
    WeixinSendUnknown,
)


def client(handler: Callable[[httpx.Request], httpx.Response]) -> WeixinILinkClient:
    return WeixinILinkClient(
        config=WeixinILinkConfig(),
        transport=httpx.MockTransport(handler),
        backoff_sleep=lambda _: asyncio.sleep(0),
    )


def credentials() -> WeixinCredentials:
    return WeixinCredentials("bot-123", "scanner-456", "secret-token", "https://ilinkai.weixin.qq.com")


def test_qr_start_and_status_preserve_protocol_shape_and_never_follow_http_redirect() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("get_bot_qrcode"):
            return httpx.Response(200, json={"qrcode": "qr-private", "qrcode_img_content": "image-content"})
        return httpx.Response(
            200, json={"status": "scaned_but_redirect", "redirect_host": "ilinkai-idc.weixin.qq.com"}
        )

    async def scenario() -> None:
        transport = client(handler)
        challenge = await transport.start_qr()
        assert challenge.qrcode == "qr-private"
        assert challenge.image_content == "image-content"
        assert "qr-private" not in repr(challenge)
        result = await transport.poll_qr(challenge)
        assert result.status is QRStatus.SCANED_BUT_REDIRECT
        assert result.poll_base_url == "https://ilinkai-idc.weixin.qq.com"
        assert requests[0].method == "POST"
        assert requests[0].url == "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
        assert requests[0].headers["ilink-app-id"] == "bot"
        assert requests[0].headers["authorizationtype"] == "ilink_bot_token"
        assert requests[0].headers.get("authorization") is None
        assert requests[0].content == b'{"local_token_list":[]}'
        assert requests[1].method == "GET"
        assert requests[1].url.params["qrcode"] == "qr-private"
        assert requests[1].headers.get("authorizationtype") is None
        await transport.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "ilinkai.weixin.qq.com.evil.example",
        "127.0.0.1",
        "ilinkai.weixin.qq.com:444",
        "ilinkai..weixin.qq.com",
        "ilinkai-x.weixin.qq.com/path",
    ],
)
def test_qr_rejects_untrusted_redirect_host(host: str) -> None:
    async def scenario() -> None:
        transport = client(
            lambda _: httpx.Response(200, json={"status": "scaned_but_redirect", "redirect_host": host})
        )
        challenge = QRChallenge("qr", "image", "https://ilinkai.weixin.qq.com")
        with pytest.raises(WeixinProtocolError):
            await transport.poll_qr(challenge)
        await transport.aclose()

    asyncio.run(scenario())


def test_qr_confirmation_is_unbound_until_exact_scanner_and_bot_match() -> None:
    payload = {
        "status": "confirmed",
        "ilink_bot_id": "bot-123",
        "ilink_user_id": "scanner-456",
        "bot_token": "secret-token",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }

    async def scenario() -> None:
        transport = client(lambda _: httpx.Response(200, json=payload))
        challenge = QRChallenge("qr", "image", "https://ilinkai.weixin.qq.com")
        result = await transport.poll_qr(challenge)
        assert result.status is QRStatus.CONFIRMED_UNBOUND
        assert result.credentials == credentials()
        assert "secret-token" not in repr(result)
        matched = await transport.poll_qr(
            challenge, expected_scanner_user_id="scanner-456", expected_bot_id="bot-123"
        )
        assert matched.status is QRStatus.CONFIRMED
        with pytest.raises(WeixinProtocolError) as error:
            await transport.poll_qr(challenge, expected_scanner_user_id="other", expected_bot_id="bot-123")
        assert "secret-token" not in str(error.value)
        with pytest.raises(WeixinProtocolError):
            await transport.poll_qr(challenge, expected_scanner_user_id="scanner-456")
        await transport.aclose()

    asyncio.run(scenario())


def test_updates_return_text_and_new_cursor_without_losing_large_message_id() -> None:
    requests: list[httpx.Request] = []
    large_id = 18_446_744_073_709_551_615

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ret": 0,
                "get_updates_buf": "cursor-2",
                "longpolling_timeout_ms": 90_000,
                "msgs": [
                    {
                        "message_id": large_id,
                        "from_user_id": "scanner-456",
                        "to_user_id": "bot-123",
                        "context_token": "context-secret",
                        "item_list": [
                            {"type": 1, "text_item": {"text": "status"}},
                            {"type": 2, "image_item": {}},
                        ],
                    },
                    {"message_id": 2, "from_user_id": "someone", "item_list": [{"type": 2}]},
                ],
            },
        )

    async def scenario() -> None:
        transport = client(handler)
        batch = await transport.get_updates(credentials(), "cursor-1")
        assert batch.cursor == "cursor-2"
        assert batch.next_timeout_seconds == 40
        assert len(batch.messages) == 1
        assert batch.messages[0].message_id == str(large_id)
        assert batch.messages[0].sender_id == "scanner-456"
        assert batch.messages[0].context_token == "context-secret"
        assert batch.messages[0].text == "status"
        assert "context-secret" not in repr(batch)
        body = requests[0].read().decode()
        assert '"get_updates_buf":"cursor-1"' in body
        assert requests[0].headers["authorization"] == "Bearer secret-token"
        await transport.aclose()

    asyncio.run(scenario())


def test_updates_retry_is_bounded_and_cancellation_propagates() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("secret-token should stay private")

    async def scenario() -> None:
        transport = client(handler)
        with pytest.raises(WeixinProtocolError) as error:
            await transport.get_updates(credentials(), "cursor")
        assert attempts == 3
        assert "secret-token" not in str(error.value)
        await transport.aclose()

    asyncio.run(scenario())


def test_send_requires_context_and_stable_id_and_reports_platform_acceptance_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ret": 0})

    async def scenario() -> None:
        transport = client(handler)
        with pytest.raises(WeixinProtocolError):
            await transport.send_text(credentials(), "scanner-456@im.wechat", "", "client-1", "hello")
        assert not requests
        receipt = await transport.send_text(
            credentials(), "scanner-456@im.wechat", "context-secret", "client-1", "hello"
        )
        assert receipt.status == "accepted_by_platform"
        assert receipt.client_id == "client-1"
        body: dict[str, Any] = json.loads(requests[0].content)
        assert body["msg"] == {
            "from_user_id": "",
            "to_user_id": "scanner-456@im.wechat",
            "client_id": "client-1",
            "message_type": 2,
            "message_state": 2,
            "context_token": "context-secret",
            "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
        }
        await transport.aclose()

    asyncio.run(scenario())


def test_send_timeout_is_unknown_and_never_retried() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("secret-token should stay private")

    async def scenario() -> None:
        transport = client(handler)
        with pytest.raises(WeixinSendUnknown) as error:
            await transport.send_text(
                credentials(), "scanner-456@im.wechat", "context-secret", "client-1", "hello"
            )
        assert attempts == 1
        assert "secret-token" not in str(error.value)
        await transport.aclose()

    asyncio.run(scenario())


def test_qr_http_redirect_is_rejected_before_following_location() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async def scenario() -> None:
        transport = client(handler)
        with pytest.raises(WeixinProtocolError):
            await transport.start_qr()
        assert len(requests) == 1
        await transport.aclose()

    asyncio.run(scenario())


def test_qr_rejects_untrusted_confirmed_base_url() -> None:
    payload = {
        "status": "confirmed",
        "ilink_bot_id": "bot-123",
        "ilink_user_id": "scanner-456",
        "bot_token": "secret-token",
        "baseurl": "http://127.0.0.1/private",
    }

    async def scenario() -> None:
        transport = client(lambda _: httpx.Response(200, json=payload))
        with pytest.raises(WeixinProtocolError):
            await transport.poll_qr(QRChallenge("qr", "image", "https://ilinkai.weixin.qq.com"))
        await transport.aclose()

    asyncio.run(scenario())


def test_malformed_text_blocks_cursor_handoff() -> None:
    payload = {
        "ret": 0,
        "get_updates_buf": "next-cursor",
        "msgs": [
            {
                "message_id": 42,
                "from_user_id": "scanner-456",
                "to_user_id": "bot-123",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            }
        ],
    }

    async def scenario() -> None:
        transport = client(lambda _: httpx.Response(200, json=payload))
        with pytest.raises(WeixinProtocolError) as error:
            await transport.get_updates(credentials(), "old-cursor")
        assert error.value.code == "invalid_ilink_message"
        await transport.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("ret", [None, False, "0"])
def test_send_requires_exact_numeric_zero(ret: object) -> None:
    async def scenario() -> None:
        transport = client(lambda _: httpx.Response(200, json={"ret": ret}))
        with pytest.raises(WeixinSendUnknown):
            await transport.send_text(credentials(), "scanner-456@im.wechat", "context", "stable-id", "hello")
        await transport.aclose()

    asyncio.run(scenario())


def test_cancelled_long_poll_propagates_without_retry() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    async def scenario() -> None:
        transport = client(handler)
        with pytest.raises(asyncio.CancelledError):
            await transport.get_updates(credentials(), "cursor")
        assert attempts == 1
        await transport.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"api_base_url": "http://ilinkai.weixin.qq.com"},
        {"api_base_url": "https://evil.example"},
        {"qr_poll_timeout_seconds": 0},
        {"max_response_bytes": 0},
        {"update_retries": 0},
        {"channel_version": "secret\nvalue"},
    ],
)
def test_config_rejects_unsafe_or_unbounded_values(changes: dict[str, object]) -> None:
    with pytest.raises(WeixinProtocolError):
        WeixinILinkConfig(**cast(Any, changes))


def test_config_controls_wire_version_and_transport_limits() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ret": 0, "msgs": []})

    async def scenario() -> None:
        transport = WeixinILinkClient(
            config=WeixinILinkConfig(
                channel_version="2.4.9",
                app_client_version=132105,
                bot_agent="OneFoundry/1.0",
                update_retries=1,
            ),
            transport=httpx.MockTransport(handler),
        )
        await transport.get_updates(credentials(), "")
        assert requests[0].headers["ilink-app-clientversion"] == "132105"
        assert json.loads(requests[0].content)["base_info"] == {
            "channel_version": "2.4.9",
            "bot_agent": "OneFoundry/1.0",
        }
        await transport.aclose()

    asyncio.run(scenario())


def test_send_rejects_recipient_outside_im_wechat_before_network() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ret": 0})

    async def scenario() -> None:
        transport = client(handler)
        for recipient in (
            "scanner-456",
            "scanner-456@im.wechat.evil.test",
            "@im.wechat",
            "other@IM.WECHAT",
        ):
            with pytest.raises(WeixinProtocolError):
                await transport.send_text(credentials(), recipient, "context", "client-1", "hello")
        assert not requests
        await transport.aclose()

    asyncio.run(scenario())


def test_unsupported_group_and_media_messages_are_skipped() -> None:
    payload = {
        "ret": 0,
        "get_updates_buf": "cursor-2",
        "msgs": [
            {"group_id": "group-1", "item_list": [{"type": 1, "text_item": {"text": "hello"}}]},
            {"item_list": [{"type": 2, "image_item": {}}]},
            {
                "message_id": 7,
                "from_user_id": "scanner@im.wechat",
                "to_user_id": "bot-123",
                "context_token": "context",
                "item_list": [{"type": 1, "text_item": {"text": "status"}}],
            },
        ],
    }

    async def scenario() -> None:
        transport = client(lambda _: httpx.Response(200, json=payload))
        batch = await transport.get_updates(credentials(), "cursor-1")
        assert [message.message_id for message in batch.messages] == ["7"]
        assert batch.cursor == "cursor-2"
        await transport.aclose()

    asyncio.run(scenario())
