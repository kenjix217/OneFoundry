"""Offline contract tests for the Discord direct-message boundary."""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

import pytest

from onefoundry.discord_dm import (
    DiscordDestinationMismatch,
    DiscordDMTransport,
    DiscordInbound,
    DiscordRawMessage,
    DiscordSendReceipt,
    _discord_client,
)


class FakeClient:
    def __init__(self) -> None:
        self.on_message: Callable[[Any], Awaitable[None]] | None = None
        self.ready = False
        self.latency = float("inf")
        self.closed = False
        self.sent: list[tuple[int, str]] = []
        self.guild_sent: list[tuple[str, str, str, str]] = []
        self.send_error: Exception | None = None
        self.start_error: Exception | None = None
        self.resolved_chat_id = "555"
        self.send_hang = False

    async def start(self, token: str) -> None:
        assert token == "private-token"
        if self.start_error:
            raise self.start_error
        self.ready = True
        self.latency = 0.2

    async def close(self) -> None:
        self.closed = True
        self.ready = False

    def is_ready(self) -> bool:
        return self.ready

    async def send_dm(self, recipient_id: int, expected_chat_id: str, text: str) -> str:
        if self.resolved_chat_id != expected_chat_id:
            raise DiscordDestinationMismatch()
        self.sent.append((recipient_id, text))
        if self.send_hang:
            await asyncio.Event().wait()
        if self.send_error:
            raise self.send_error
        return "888"

    async def send_guild(self, guild_id: str, channel_id: str, application_id: str, text: str) -> str:
        self.guild_sent.append((guild_id, channel_id, application_id, text))
        if self.send_error:
            raise self.send_error
        return "889"


class BlockingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def start(self, token: str) -> None:
        await super().start(token)
        self.entered.set()
        await asyncio.Event().wait()


class Message:
    def __init__(
        self,
        *,
        author_id: int = 42,
        content: str = "status",
        message_id: int = 77,
        dm: bool = True,
        bot: bool = False,
        webhook_id: int | None = None,
    ) -> None:
        self.raw = DiscordRawMessage(
            str(message_id), str(author_id), "555", content, dm, bot, webhook_id is not None
        )


def make_transport(client: FakeClient, received: list[DiscordInbound]) -> DiscordDMTransport:
    async def receive(message: DiscordInbound) -> None:
        received.append(message)

    return DiscordDMTransport(
        operator_user_id=42,
        token="private-token",
        max_text_chars=1900,
        send_timeout_seconds=15,
        on_inbound=receive,
        client_factory=lambda callback: bind(client, callback),
    )


def bind(client: FakeClient, callback: Callable[[Any], Awaitable[None]]) -> FakeClient:
    client.on_message = callback
    return client


def test_only_bound_human_dm_is_forwarded_with_platform_id() -> None:
    async def scenario() -> None:
        client = FakeClient()
        received: list[DiscordInbound] = []
        transport = make_transport(client, received)
        await transport.start()
        assert client.on_message is not None
        for message in [
            Message(author_id=43),
            Message(dm=False),
            Message(bot=True),
            Message(webhook_id=9),
            Message(content=""),
            Message(content="x" * 1901),
        ]:
            await client.on_message(message.raw)
        await client.on_message(Message().raw)
        await client.on_message(Message().raw)
        assert received == [
            DiscordInbound("77", "42", "555", "status"),
            DiscordInbound("77", "42", "555", "status"),
        ]
        await transport.stop()

    asyncio.run(scenario())


def test_server_channel_accepts_only_operator_mention_in_exact_server_and_channel() -> None:
    async def scenario() -> None:
        client = FakeClient()
        received: list[DiscordInbound] = []

        async def receive(message: DiscordInbound) -> None:
            received.append(message)

        transport = DiscordDMTransport(
            operator_user_id=42,
            token="private-token",
            max_text_chars=1900,
            send_timeout_seconds=15,
            on_inbound=receive,
            guild_id="777",
            channel_id="555",
            application_id="999",
            client_factory=lambda callback: bind(client, callback),
        )
        await transport.start()
        assert client.on_message is not None
        base = DiscordRawMessage("77", "42", "555", "<@999> Start work", False, False, False, "777", "999")
        from dataclasses import replace

        for bad in (
            replace(base, author_id="43"),
            replace(base, channel_id="556"),
            replace(base, guild_id="778"),
            replace(base, mentioned_bot_id=None),
            replace(base, is_bot=True),
            replace(base, is_webhook=True),
            replace(base, text="<@999>"),
            replace(base, is_dm=True),
        ):
            await client.on_message(bad)
        await client.on_message(base)
        assert received == [DiscordInbound("77", "42", "555", "Start work", "777")]
        assert await transport.send("Accepted", "555") == DiscordSendReceipt(
            "accepted_by_platform", "889", None
        )
        assert await transport.send("Wrong", "556") == DiscordSendReceipt(
            "failed", None, "destination_mismatch"
        )
        assert client.guild_sent == [("777", "555", "999", "Accepted")]
        await transport.stop()

    asyncio.run(scenario())


def test_send_distinguishes_accepted_from_unknown_and_checks_live_status() -> None:
    async def scenario() -> None:
        client = FakeClient()
        transport = make_transport(client, [])
        assert transport.status().connected is False
        assert await transport.send("hello", "555") == DiscordSendReceipt("failed", None, "disconnected")
        await transport.start()
        assert transport.status().connected is True
        assert await transport.send("hello", "555") == DiscordSendReceipt("accepted_by_platform", "888", None)
        assert client.sent == [(42, "hello")]
        client.resolved_chat_id = "999"
        assert await transport.send("wrong DM", "555") == DiscordSendReceipt(
            "failed", None, "destination_mismatch"
        )
        assert client.sent == [(42, "hello")]
        client.resolved_chat_id = "555"
        client.send_error = RuntimeError("private-token should not leak")
        assert await transport.send("again", "555") == DiscordSendReceipt("unknown", None, "delivery_unknown")
        client.latency = float("inf")
        assert transport.status().connected is False
        await transport.stop()

    asyncio.run(scenario())


def test_invalid_binding_and_gateway_failure_fail_closed_without_secret_details() -> None:
    client = FakeClient()
    with pytest.raises(ValueError):
        DiscordDMTransport(
            operator_user_id=0,
            token="private-token",
            max_text_chars=1900,
            send_timeout_seconds=15,
            on_inbound=lambda _: done(),
            client_factory=lambda callback: bind(client, callback),
        )
    with pytest.raises(ValueError):
        DiscordDMTransport(
            operator_user_id="42",  # type: ignore[arg-type]
            token="private-token",
            max_text_chars=1900,
            send_timeout_seconds=15,
            on_inbound=lambda _: done(),
            client_factory=lambda callback: bind(client, callback),
        )

    async def scenario() -> None:
        transport = make_transport(client, [])
        client.start_error = RuntimeError("private-token")
        with pytest.raises(RuntimeError, match="Discord connection failed") as error:
            await transport.start()
        assert "private-token" not in str(error.value)
        assert transport.status().error_code == "connection_failed"
        client.start_error = None
        await transport.start()
        assert transport.status().connected
        await transport.stop()
        assert not transport.status().connected

    asyncio.run(scenario())


def test_hung_discord_send_is_bounded_and_unknown() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.send_hang = True
        transport = DiscordDMTransport(
            operator_user_id=42,
            token="private-token",
            max_text_chars=1900,
            send_timeout_seconds=0.01,
            on_inbound=lambda _: done(),
            client_factory=lambda callback: bind(client, callback),
        )
        await transport.start()
        assert await transport.send("reply", "555") == DiscordSendReceipt("unknown", None, "delivery_unknown")
        assert client.sent == [(42, "reply")]
        await transport.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, -1, 2001, True, "1900"])
def test_invalid_text_limit_is_rejected(limit: object) -> None:
    with pytest.raises(ValueError, match="max_text_chars"):
        DiscordDMTransport(
            operator_user_id=42,
            token="private-token",
            max_text_chars=limit,  # type: ignore[arg-type]
            send_timeout_seconds=15,
            on_inbound=lambda _: done(),
            client_factory=lambda callback: bind(FakeClient(), callback),
        )


def test_configured_text_limit_applies_to_inbound_and_outbound() -> None:
    async def scenario() -> None:
        client = FakeClient()
        received: list[DiscordInbound] = []

        async def receive(message: DiscordInbound) -> None:
            received.append(message)

        transport = DiscordDMTransport(
            operator_user_id=42,
            token="private-token",
            max_text_chars=5,
            send_timeout_seconds=15,
            on_inbound=receive,
            client_factory=lambda callback: bind(client, callback),
        )
        await transport.start()
        assert client.on_message is not None
        await client.on_message(Message(content="123456").raw)
        await client.on_message(Message(content="12345").raw)
        assert [item.text for item in received] == ["12345"]
        assert await transport.send("123456", "555") == DiscordSendReceipt("failed", None, "invalid_text")
        assert await transport.send("12345", "555") == DiscordSendReceipt("accepted_by_platform", "888", None)

    asyncio.run(scenario())


async def done() -> None:
    return None


def test_cancelled_gateway_closes_socket() -> None:
    async def scenario() -> None:
        client = BlockingClient()
        transport = make_transport(client, [])
        task = asyncio.create_task(transport.start())
        await client.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed
        assert not transport.status().connected

    asyncio.run(scenario())


def test_sdk_requests_dm_events_without_privileged_message_content(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = ModuleType("discord")
    sent: list[str] = []

    class Intents:
        dm_messages = False
        message_content = False
        guild_messages = False
        guilds = False

        @classmethod
        def none(cls) -> "Intents":
            return cls()

    class Client:
        def __init__(self, *, intents: Intents, allowed_mentions: object) -> None:
            self.intents = intents

        async def fetch_user(self, recipient_id: int) -> "User":
            assert recipient_id == 42
            return User()

    class Channel:
        id = 999

        async def send(self, value: str, **_kwargs: object) -> object:
            sent.append(value)
            return object()

    class User:
        async def create_dm(self) -> Channel:
            return Channel()

    class AllowedMentions:
        @staticmethod
        def none() -> object:
            return object()

    sdk.Intents = Intents  # type: ignore[attr-defined]
    sdk.Client = Client  # type: ignore[attr-defined]
    sdk.AllowedMentions = AllowedMentions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "discord", sdk)

    async def receive(_: DiscordRawMessage) -> None:
        return None

    client = _discord_client(receive)
    assert client.intents.dm_messages  # type: ignore[attr-defined]
    assert not client.intents.message_content  # type: ignore[attr-defined]
    assert client.intents.guild_messages  # type: ignore[attr-defined]
    assert client.intents.guilds  # type: ignore[attr-defined]
    with pytest.raises(DiscordDestinationMismatch):
        asyncio.run(client.send_dm(42, "555", "reply"))
    assert sent == []
