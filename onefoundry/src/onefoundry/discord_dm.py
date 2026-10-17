"""Discord personal-DM and mention-gated server-channel transport boundary.

The channel core owns durable replay protection and outbound claims. This module
only checks SDK-origin identity, maintains a live gateway connection, and reports
whether Discord accepted a send. It never starts a conversation by itself.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

DISCORD_MESSAGE_CHAR_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class DiscordRawMessage:
    message_id: str
    author_id: str
    channel_id: str
    text: str
    is_dm: bool
    is_bot: bool
    is_webhook: bool
    guild_id: str | None = None
    mentioned_bot_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordInbound:
    platform_message_id: str
    sender_id: str
    chat_id: str
    text: str
    guild_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordSendReceipt:
    status: str  # accepted_by_platform, failed, or unknown
    remote_message_id: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class DiscordStatus:
    connected: bool
    error_code: str | None


class DiscordDestinationMismatch(Exception):
    """The SDK resolved a different destination before any send was attempted."""


class DiscordClient(Protocol):
    @property
    def latency(self) -> float: ...

    async def start(self, token: str) -> None: ...

    async def close(self) -> None: ...

    def is_ready(self) -> bool: ...

    async def send_dm(self, recipient_id: int, expected_chat_id: str, text: str) -> str: ...

    async def send_guild(self, guild_id: str, channel_id: str, application_id: str, text: str) -> str: ...


type InboundCallback = Callable[[DiscordInbound], Awaitable[None]]
type ClientFactory = Callable[[Callable[[DiscordRawMessage], Awaitable[None]]], DiscordClient]


def _numeric_id(value: int | str) -> bool:
    return str(value).isascii() and str(value).isdecimal() and int(value) > 0


class DiscordDMTransport:
    """One bot connection bound to one numeric operator ID and its DM."""

    def __init__(
        self,
        *,
        operator_user_id: int,
        token: str,
        max_text_chars: int,
        send_timeout_seconds: float,
        on_inbound: InboundCallback,
        client_factory: ClientFactory | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        application_id: str | None = None,
    ) -> None:
        if type(operator_user_id) is not int or not _numeric_id(operator_user_id):
            raise ValueError("operator_user_id must be a positive numeric Discord ID")
        if not token or token.isspace():
            raise ValueError("Discord bot token is required")
        if type(max_text_chars) is not int or not 1 <= max_text_chars <= DISCORD_MESSAGE_CHAR_LIMIT:
            raise ValueError("max_text_chars must be between 1 and Discord's message character limit")
        if (
            isinstance(send_timeout_seconds, bool)
            or not isinstance(send_timeout_seconds, (int, float))
            or not math.isfinite(send_timeout_seconds)
            or not 0 < send_timeout_seconds <= 60
        ):
            raise ValueError("send_timeout_seconds must be positive and at most 60")
        self._operator_user_id = operator_user_id
        if guild_id is not None and not all(
            _numeric_id(value) for value in (guild_id, channel_id or "", application_id or "")
        ):
            raise ValueError("Discord server mode requires numeric server, channel, and application IDs")
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._application_id = application_id
        self._token = token
        self._max_text_chars = max_text_chars
        self._send_timeout_seconds = send_timeout_seconds
        self._on_inbound = on_inbound
        self._client = (client_factory or _discord_client)(self._handle_message)
        self._error_code: str | None = None
        self._stopped = False

    async def start(self) -> None:
        """Run the Discord gateway until stopped; caller owns its task lifecycle."""
        self._stopped = False
        self._error_code = None
        try:
            await self._client.start(self._token)
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception:
            self._error_code = "connection_failed"
            raise RuntimeError("Discord connection failed") from None

    async def stop(self) -> None:
        self._stopped = True
        await self._client.close()

    def status(self) -> DiscordStatus:
        connected = (
            not self._stopped
            and self._client.is_ready()
            and math.isfinite(self._client.latency)
            and self._client.latency >= 0
        )
        return DiscordStatus(connected, None if connected else self._error_code)

    async def _handle_message(self, message: DiscordRawMessage) -> None:
        if self._guild_id is None:
            correct_route = message.is_dm and message.guild_id is None
            body = message.text
        else:
            correct_route = (
                not message.is_dm
                and message.guild_id == self._guild_id
                and message.channel_id == self._channel_id
                and message.mentioned_bot_id == self._application_id
            )
            body = re.sub(rf"<@!?{self._application_id}>", "", message.text).strip()
        if (
            not correct_route
            or message.is_bot
            or message.is_webhook
            or message.author_id != str(self._operator_user_id)
            or not _numeric_id(message.message_id)
            or not _numeric_id(message.channel_id)
            or not body.strip()
            or len(body) > self._max_text_chars
        ):
            return
        await self._on_inbound(
            DiscordInbound(message.message_id, message.author_id, message.channel_id, body, message.guild_id)
        )

    async def send(self, text: str, expected_chat_id: str) -> DiscordSendReceipt:
        if not text.strip() or len(text) > self._max_text_chars:
            return DiscordSendReceipt("failed", None, "invalid_text")
        if not _numeric_id(expected_chat_id):
            return DiscordSendReceipt("failed", None, "invalid_destination")
        if self._guild_id is not None and expected_chat_id != self._channel_id:
            return DiscordSendReceipt("failed", None, "destination_mismatch")
        if not self.status().connected:
            return DiscordSendReceipt("failed", None, "disconnected")
        try:
            message_id = await asyncio.wait_for(
                self._client.send_dm(self._operator_user_id, expected_chat_id, text)
                if self._guild_id is None
                else self._client.send_guild(
                    self._guild_id, expected_chat_id, self._application_id or "", text
                ),
                timeout=self._send_timeout_seconds,
            )
        except DiscordDestinationMismatch:
            return DiscordSendReceipt("failed", None, "destination_mismatch")
        except Exception:
            # An SDK exception after dispatch can mean accepted but acknowledgement lost.
            return DiscordSendReceipt("unknown", None, "delivery_unknown")
        return DiscordSendReceipt("accepted_by_platform", message_id, None)


def _discord_client(on_message: Callable[[DiscordRawMessage], Awaitable[None]]) -> DiscordClient:
    """Import discord.py only when a Discord connection is configured."""
    try:
        import discord
    except ImportError as exc:
        raise RuntimeError("Discord transport requires discord.py") from exc

    intents = discord.Intents.none()
    intents.dm_messages = True
    intents.guild_messages = True
    intents.guilds = True

    class Client(discord.Client):
        async def on_message(self, message: discord.Message) -> None:
            await on_message(
                DiscordRawMessage(
                    message_id=str(message.id),
                    author_id=str(message.author.id),
                    channel_id=str(message.channel.id),
                    text=message.content,
                    is_dm=isinstance(message.channel, discord.DMChannel),
                    is_bot=message.author.bot,
                    is_webhook=message.webhook_id is not None,
                    guild_id=str(message.guild.id) if message.guild else None,
                    mentioned_bot_id=(
                        str(self.user.id)
                        if self.user and any(user.id == self.user.id for user in message.mentions)
                        else None
                    ),
                )
            )

        async def send_dm(self, recipient_id: int, expected_chat_id: str, text: str) -> str:
            user = await self.fetch_user(recipient_id)
            channel = await user.create_dm()
            if str(channel.id) != expected_chat_id:
                raise DiscordDestinationMismatch()
            sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return str(sent.id)

        async def send_guild(self, guild_id: str, channel_id: str, application_id: str, text: str) -> str:
            if self.user is None or str(self.user.id) != application_id:
                raise DiscordDestinationMismatch()
            channel = await self.fetch_channel(int(channel_id))
            if (
                not isinstance(channel, discord.TextChannel)
                or str(channel.id) != channel_id
                or str(channel.guild.id) != guild_id
            ):
                raise DiscordDestinationMismatch()
            sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return str(sent.id)

    return Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
