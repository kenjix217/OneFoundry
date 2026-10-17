"""Process-local lifecycle for configured remote DM transports.

The database core owns every durable message and send decision. This module
never retries an outbound claim after an ambiguous transport result.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.discord_dm import DiscordDMTransport, DiscordInbound
from onefoundry.remote_channel_core import OutboundResult, RemoteChannelCore, ReplyPolicy
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict, NotFound, Unavailable
from onefoundry.weixin_ilink import (
    QRChallenge,
    QRStatus,
    WeixinCredentials,
    WeixinILinkClient,
    WeixinILinkConfig,
    WeixinProtocolError,
    WeixinSendUnknown,
)


@dataclass(frozen=True)
class ChannelSnapshot:
    project_id: UUID
    id: UUID
    platform: str
    sender_id: str | None
    chat_id: str | None
    bot_id: str | None
    enabled: bool
    identity_confirmed: bool
    credential_ready: bool
    credential_revision: int
    work_order_id: UUID | None
    profile_id: UUID | None
    guild_id: str | None = None


@dataclass
class PairingSession:
    session_id: UUID
    project_id: UUID
    channel_id: UUID
    challenge: QRChallenge | None
    expires_at: datetime
    profile_id: UUID
    reply_policy: ReplyPolicy
    status: str = "waiting"
    challenge_id: UUID | None = None
    scanner_id: str | None = None
    bot_id: str | None = None
    verify_code: str | None = None


def _credential_envelope(credentials: WeixinCredentials) -> str:
    payload = json.dumps(
        {
            "bot_id": credentials.bot_id,
            "scanner_id": credentials.scanner_user_id,
            "token": credentials.token,
            "base_url": credentials.base_url,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _qr_payload(challenge: QRChallenge) -> str:
    """Only the Tencent QR landing URL, tied to the opaque poll token, is scannable."""
    value = challenge.image_content
    if len(value) > 4096:
        raise WeixinProtocolError("invalid_qr_image_content")
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise WeixinProtocolError("invalid_qr_image_content") from None
    if (
        parsed.geturl() != value
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or parsed.scheme != "https"
        or parsed.netloc != "liteapp.weixin.qq.com"
        or not parsed.path.startswith("/q/")
        or not parsed.path[3:]
        or any(not (char.isascii() and char.isalnum()) for char in parsed.path[3:])
        or parsed.fragment
        or len(query) != 2
        or dict(query) != {"qrcode": challenge.qrcode, "bot_type": "3"}
    ):
        raise WeixinProtocolError("invalid_qr_image_content")
    return value


def _decode_credential(value: str) -> WeixinCredentials:
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"bot_id", "scanner_id", "token", "base_url"}:
            raise ValueError
        credential = WeixinCredentials(
            **{
                "bot_id": data["bot_id"],
                "scanner_user_id": data["scanner_id"],
                "token": data["token"],
                "base_url": data["base_url"],
            }
        )
        # The transport validates the Tencent host, token, and identifiers before any request.
        from onefoundry.weixin_ilink import _base_url, _identifier

        _identifier(credential.bot_id, "invalid_bot_id")
        _identifier(credential.scanner_user_id, "invalid_scanner_id")
        _identifier(credential.token, "invalid_ilink_token", limit=4096)
        _base_url(credential.base_url)
        return credential
    except (ValueError, TypeError, KeyError, binascii.Error, WeixinProtocolError):
        raise WeixinProtocolError("invalid_stored_ilink_credential") from None


class RemoteChannelRuntime:
    def __init__(
        self,
        core: RemoteChannelCore,
        settings: Settings,
        *,
        discord_factory: Callable[..., DiscordDMTransport] = DiscordDMTransport,
        weixin_factory: Callable[..., WeixinILinkClient] = WeixinILinkClient,
        maintenance: object | None = None,
    ) -> None:
        self.core = core
        self.settings = settings
        self.maintenance = maintenance
        self._discord_factory = discord_factory
        self._weixin_factory = weixin_factory
        self._supervisor: asyncio.Task[None] | None = None
        self._inbox_worker: asyncio.Task[None] | None = None
        self._workers: dict[tuple[UUID, UUID], tuple[ChannelSnapshot, asyncio.Task[None]]] = {}
        self._sessions: dict[UUID, PairingSession] = {}
        self._pairing_client: WeixinILinkClient | None = None
        self._pairing_lock = asyncio.Lock()
        self._heartbeats: dict[tuple[UUID, UUID], tuple[bool, str | None, datetime]] = {}
        self.reconciled = False

    def _guarded(self, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance):
            with self.maintenance.guard():
                return call(*args, **kwargs)
        return call(*args, **kwargs)

    def _reconcile_startup(self) -> None:
        """Settle interrupted records while new work remains fenced during restart."""
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance):
            with self.maintenance.guard(allow_maintenance=True):
                self.core.reconcile_interrupted()
        else:
            self.core.reconcile_interrupted()

    def _weixin_config(self) -> WeixinILinkConfig:
        s = self.settings
        return WeixinILinkConfig(
            api_base_url=s.weixin_api_base_url,
            channel_version=s.weixin_channel_version,
            app_client_version=s.weixin_app_client_version,
            bot_agent=s.weixin_bot_agent,
            qr_request_timeout_seconds=s.weixin_qr_request_timeout_seconds,
            qr_poll_timeout_seconds=s.weixin_qr_poll_timeout_seconds,
            poll_timeout_seconds=s.weixin_poll_timeout_seconds,
            max_poll_timeout_seconds=s.weixin_max_poll_timeout_seconds,
            send_timeout_seconds=s.weixin_send_timeout_seconds,
            update_retries=s.weixin_update_retries,
            retry_backoff_seconds=s.weixin_retry_backoff_seconds,
            max_response_bytes=s.weixin_max_response_bytes,
            max_cursor_length=s.weixin_max_cursor_length,
            max_text_length=s.weixin_max_text_length,
        )

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        self._supervisor = asyncio.create_task(self._supervise(), name="remote-channel-supervisor")
        self._inbox_worker = asyncio.create_task(self._process_inbox(), name="project-chief-inbox")

    async def stop(self) -> None:
        if self._inbox_worker is not None:
            self._inbox_worker.cancel()
            await asyncio.gather(self._inbox_worker, return_exceptions=True)
            self._inbox_worker = None
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
            self._supervisor = None
        for _, task in self._workers.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in self._workers.values()), return_exceptions=True)
        self._workers.clear()
        for session in self._sessions.values():
            if session.challenge_id is not None:
                try:
                    await self._expire_pairing(session)
                except (SQLAlchemyError, Unavailable):
                    session.challenge = None
        if self._pairing_client is not None:
            await self._pairing_client.aclose()
            self._pairing_client = None
        self._sessions.clear()

    async def _process_inbox(self) -> None:
        while True:
            try:
                if self.reconciled and self.core.gateway._task_conversations_reconciled:
                    progressed = await asyncio.to_thread(self._guarded, self.core.process_one_project_message)
                    if progressed:
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # A saved running turn becomes unknown on restart; never resend it here.
                logging.getLogger(__name__).error("Chief inbox worker failed; inspect saved turn state")
            await asyncio.sleep(self.settings.remote_channel_poll_seconds)

    def _snapshots(self) -> list[ChannelSnapshot]:
        with self.core.gateway.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT c.project_id,c.id,c.platform,c.sender_id,c.chat_id,c.bot_id,c.enabled,"
                    "c.identity_confirmed,c.credential_ready,c.credential_revision,"
                    "c.work_order_id,c.profile_id,c.guild_id "
                    "FROM remote_channels c JOIN projects p ON p.id=c.project_id "
                    "WHERE p.status='active'"
                )
            ).mappings()
            return [ChannelSnapshot(**dict(row)) for row in rows]

    def _needed(self, snapshot: ChannelSnapshot) -> bool:
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance) and self.maintenance.active():
            return False
        if not self.settings.remote_channel_network_enabled:
            return False
        if not snapshot.credential_ready or not snapshot.sender_id:
            return False
        if snapshot.platform == "discord":
            return (
                snapshot.sender_id.isascii()
                and snapshot.sender_id.isdecimal()
                and int(snapshot.sender_id) > 0
                and (snapshot.guild_id is None or bool(snapshot.chat_id and snapshot.bot_id))
            )
        return snapshot.platform == "wechat" and snapshot.enabled and snapshot.identity_confirmed

    async def _supervise(self) -> None:
        while True:
            try:
                if not self.reconciled:
                    await asyncio.to_thread(self._reconcile_startup)
                    self.reconciled = True
                if not self.core.gateway._task_conversations_reconciled:
                    await asyncio.sleep(self.settings.remote_channel_poll_seconds)
                    continue
                await self._prune_pairings()
                snapshots = await asyncio.to_thread(self._snapshots)
                wanted = {(row.project_id, row.id): row for row in snapshots if self._needed(row)}
                for key, (old, task) in list(self._workers.items()):
                    if wanted.get(key) != old or task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        del self._workers[key]
                for key, row in wanted.items():
                    if key not in self._workers:
                        worker = self._run_discord if row.platform == "discord" else self._run_weixin
                        self._workers[key] = (row, asyncio.create_task(worker(row)))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed DB scan must not terminate unrelated live channels.
                await asyncio.sleep(self.settings.remote_channel_poll_seconds)
            await asyncio.sleep(self.settings.remote_channel_poll_seconds)

    async def _heartbeat(self, row: ChannelSnapshot, connected: bool, code: str | None = None) -> None:
        key = (row.project_id, row.id)
        old = self._heartbeats.get(key)
        now = datetime.now(UTC)
        if old and old[:2] == (connected, code) and (now - old[2]).total_seconds() < 60:
            return
        try:
            await asyncio.to_thread(
                self._guarded, self.core.heartbeat, row.project_id, row.id, connected, code
            )
            self._heartbeats[key] = (connected, code, now)
        except (Conflict, NotFound):
            pass

    async def _run_discord(self, row: ChannelSnapshot) -> None:
        lock = asyncio.Lock()
        delay = 1
        while True:
            transport: DiscordDMTransport | None = None
            task: asyncio.Task[None] | None = None
            current: list[DiscordDMTransport] = []
            try:
                credential = await asyncio.to_thread(
                    self._guarded, self.core.read_credential, row.project_id, row.id
                )

                async def inbound(
                    message: DiscordInbound, current: list[DiscordDMTransport] = current
                ) -> None:
                    async with lock:
                        state = await asyncio.to_thread(
                            self._guarded, self.core.status, row.project_id, row.id
                        )
                        assert isinstance(state, dict)
                        if not state["enabled"]:
                            if state["identity_confirmed"]:
                                return
                            await asyncio.to_thread(
                                self.core.observe_identity,
                                row.project_id,
                                row.id,
                                message.sender_id,
                                message.chat_id,
                                message.guild_id is None,
                                message.guild_id,
                            )
                            return
                        result = await asyncio.to_thread(
                            self._guarded,
                            self.core.receive,
                            row.project_id,
                            row.id,
                            message.platform_message_id,
                            message.sender_id,
                            message.chat_id,
                            message.text,
                            message.guild_id is None,
                            None,
                            message.guild_id,
                        )
                        if result.get("outbound_id") and not result.get("duplicate"):
                            await self._send_discord(row, current[0], result["outbound_id"])

                transport = self._discord_factory(
                    operator_user_id=int(row.sender_id or "0"),
                    token=credential,
                    max_text_chars=self.settings.discord_max_text_chars,
                    send_timeout_seconds=self.settings.discord_send_timeout_seconds,
                    on_inbound=inbound,
                    guild_id=row.guild_id,
                    channel_id=row.chat_id if row.guild_id else None,
                    application_id=row.bot_id if row.guild_id else None,
                )
                current.append(transport)
                task = asyncio.create_task(transport.start())
                last: bool | None = None
                while not task.done():
                    await asyncio.sleep(self.settings.remote_channel_poll_seconds)
                    connected = transport.status().connected and row.enabled and row.identity_confirmed
                    if connected != last:
                        code = None if connected or not row.enabled else "connecting"
                        await self._heartbeat(row, connected, code)
                        last = connected
                    if connected:
                        pending = await asyncio.to_thread(
                            self._guarded, self.core.pending_outbound_ids, row.project_id, row.id
                        )
                        for outbound_id in pending:
                            await self._send_discord(row, transport, outbound_id)
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._heartbeat(row, False, "connection_failed")
            finally:
                if transport is not None:
                    await transport.stop()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await self._heartbeat(row, False, "stopped")
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def _send_discord(
        self, row: ChannelSnapshot, transport: DiscordDMTransport, outbound_id: UUID
    ) -> None:
        try:
            claim = await asyncio.to_thread(
                self._guarded, self.core.claim_outbound, row.project_id, outbound_id
            )
        except Conflict:
            await asyncio.to_thread(
                self._guarded,
                self.core.reject_pending_outbound,
                row.project_id,
                outbound_id,
                "claim_rejected",
            )
            return  # Approval-bound and already claimed replies are never sent here.
        status: OutboundResult = "unknown"
        remote_id = None
        reason: str | None = "delivery_unknown"
        try:
            receipt = await transport.send(claim["text"], claim["destination_id"])
            status = cast(
                OutboundResult,
                (
                    receipt.status
                    if receipt.status in {"accepted_by_platform", "failed", "unknown"}
                    else "unknown"
                ),
            )
            remote_id, reason = receipt.remote_message_id, receipt.error_code
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self._guarded,
                    self.core.finish_outbound,
                    row.project_id,
                    outbound_id,
                    "unknown",
                    None,
                    reason,
                )
            )
            raise
        except Exception:
            status = "unknown"
        await asyncio.to_thread(
            self._guarded, self.core.finish_outbound, row.project_id, outbound_id, status, remote_id, reason
        )

    async def _run_weixin(self, row: ChannelSnapshot) -> None:
        delay = 1
        while True:
            client: WeixinILinkClient | None = None
            try:
                stored = await asyncio.to_thread(
                    self._guarded, self.core.read_credential, row.project_id, row.id
                )
                credentials = _decode_credential(stored)
                if credentials.bot_id != row.bot_id or credentials.scanner_user_id != row.sender_id:
                    raise WeixinProtocolError("stored_ilink_identity_mismatch")
                client = self._weixin_factory(config=self._weixin_config())
                cursor = await asyncio.to_thread(self._guarded, self.core.load_cursor, row.project_id, row.id)
                next_timeout = self.settings.weixin_poll_timeout_seconds
                while True:
                    batch = await client.get_updates(credentials, cursor, timeout_seconds=next_timeout)
                    accepted: list[str] = []
                    for message in batch.messages:
                        if (
                            not message.is_dm
                            or message.sender_id != row.sender_id
                            or message.recipient_id != row.bot_id
                        ):
                            continue
                        result = await asyncio.to_thread(
                            self._guarded,
                            self.core.receive,
                            row.project_id,
                            row.id,
                            message.message_id,
                            message.sender_id,
                            message.sender_id,
                            message.text,
                            True,
                            message.context_token,
                        )
                        accepted.append(message.message_id)
                        if result.get("outbound_id") and not result.get("duplicate"):
                            await self._send_weixin(row, client, credentials, result["outbound_id"])
                    if cursor != batch.cursor or accepted:
                        await asyncio.to_thread(
                            self._guarded,
                            self.core.save_cursor,
                            row.project_id,
                            row.id,
                            cursor,
                            batch.cursor,
                            accepted,
                        )
                    cursor = batch.cursor
                    pending = await asyncio.to_thread(
                        self._guarded, self.core.pending_outbound_ids, row.project_id, row.id
                    )
                    for outbound_id in pending:
                        await self._send_weixin(row, client, credentials, outbound_id)
                    next_timeout = batch.next_timeout_seconds
                    await self._heartbeat(row, True)
                    delay = 1
            except asyncio.CancelledError:
                raise
            except WeixinProtocolError as exc:
                await self._heartbeat(row, False, exc.code)
            except Exception:
                await self._heartbeat(row, False, "poll_failed")
            finally:
                if client is not None:
                    await client.aclose()
                await self._heartbeat(row, False, "stopped")
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def _send_weixin(
        self,
        row: ChannelSnapshot,
        client: WeixinILinkClient,
        credentials: WeixinCredentials,
        outbound_id: UUID,
    ) -> None:
        try:
            claim = await asyncio.to_thread(
                self._guarded, self.core.claim_outbound, row.project_id, outbound_id
            )
        except Conflict:
            await asyncio.to_thread(
                self._guarded,
                self.core.reject_pending_outbound,
                row.project_id,
                outbound_id,
                "claim_rejected",
            )
            return
        status: OutboundResult = "unknown"
        remote_id = None
        reason: str | None = "delivery_unknown"
        try:
            receipt = await client.send_text(
                credentials,
                claim["destination_id"],
                claim["context_token"],
                str(outbound_id),
                claim["text"],
            )
            status, remote_id, reason = receipt.status, receipt.client_id, None
        except WeixinSendUnknown:
            pass
        except WeixinProtocolError as exc:
            status, reason = "failed", exc.code
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    self._guarded,
                    self.core.finish_outbound,
                    row.project_id,
                    outbound_id,
                    "unknown",
                    None,
                    reason,
                )
            )
            raise
        except Exception:
            status = "unknown"
        await asyncio.to_thread(
            self._guarded, self.core.finish_outbound, row.project_id, outbound_id, status, remote_id, reason
        )

    async def start_pairing(
        self,
        project_id: UUID,
        profile_id: UUID,
        reply_policy: ReplyPolicy = "bound_dm",
    ) -> dict[str, Any]:
        if not self.settings.remote_channel_network_enabled:
            raise Conflict("remote channel network is disabled")
        if reply_policy != "bound_dm":
            raise ValueError("WeChat pairing currently requires bound_dm reply policy")
        statuses = await asyncio.to_thread(self._guarded, self.core.status, project_id)
        assert isinstance(statuses, list)
        channel = next((row for row in statuses if row["platform"] == "wechat"), None)
        if channel is not None and channel["enabled"]:
            raise Conflict("disable the WeChat channel before new pairing")
        await asyncio.to_thread(self._guarded, self.core.validate_pairing_route, project_id, profile_id)
        async with self._pairing_lock:
            if self._pairing_client is None:
                self._pairing_client = self._weixin_factory(config=self._weixin_config())
            challenge = await self._pairing_client.start_qr()
            qr_value = _qr_payload(challenge)
            session_id = uuid4()
            expires = datetime.now(UTC) + timedelta(
                seconds=min(300, self.settings.remote_identity_challenge_seconds)
            )
            self._sessions[session_id] = PairingSession(
                session_id,
                project_id,
                channel["id"] if channel else uuid4(),
                challenge,
                expires,
                profile_id,
                reply_policy,
            )
            for old in self._sessions.values():
                if old is not self._sessions[session_id] and (
                    old.project_id == project_id or old.expires_at <= datetime.now(UTC)
                ):
                    await self._expire_pairing(old)
                    old.status = "expired"
                    old.challenge = None
                    old.verify_code = None
            self._sessions = {
                key: value for key, value in self._sessions.items() if value.expires_at > datetime.now(UTC)
            }
        # Generate the QR locally. Never send a Tencent image URL to the browser.
        import qrcode  # type: ignore[import-untyped]
        import qrcode.image.svg  # type: ignore[import-untyped]

        image = qrcode.make(qr_value, image_factory=qrcode.image.svg.SvgPathImage)
        svg = image.to_string()
        image_url = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
        return {"session_id": session_id, "qr_image_data_url": image_url, "expires_at": expires}

    async def poll_pairing(self, project_id: UUID, session_id: UUID) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None or session.project_id != project_id:
            raise NotFound("pairing session not found")
        if session.expires_at <= datetime.now(UTC):
            session.status = "expired"
            session.challenge = None
            session.verify_code = None
            await self._expire_pairing(session)
        if session.status in {"expired", "failed", "confirmed"}:
            return self._pairing_result(session)
        async with self._pairing_lock:
            assert self._pairing_client is not None
            if session.challenge is None:
                session.status = "expired"
                return self._pairing_result(session)
            try:
                verify_code = session.verify_code
                session.verify_code = None
                result = await self._pairing_client.poll_qr(session.challenge, verify_code=verify_code)
                session.challenge = QRChallenge(
                    session.challenge.qrcode, session.challenge.image_content, result.poll_base_url
                )
                if result.status in {QRStatus.SCANED, QRStatus.SCANED_BUT_REDIRECT}:
                    session.status = "scanned"
                elif result.status is QRStatus.NEED_VERIFYCODE:
                    session.status = "needs_code"
                elif result.status in {QRStatus.EXPIRED, QRStatus.VERIFY_CODE_BLOCKED}:
                    session.status = "expired"
                    session.challenge = None
                elif result.credentials is not None:
                    credential = result.credentials
                    configured = await asyncio.to_thread(
                        self._guarded,
                        self.core.configure,
                        project_id,
                        "wechat",
                        sender_id=credential.scanner_user_id,
                        chat_id=credential.scanner_user_id,
                        bot_id=credential.bot_id,
                        work_order_id=None,
                        profile_id=session.profile_id,
                        reply_policy=session.reply_policy,
                        enabled=False,
                    )
                    session.channel_id = configured["id"]
                    session.challenge_id = await asyncio.to_thread(
                        self._guarded,
                        self.core.stage_pairing,
                        project_id,
                        session.channel_id,
                        credential.scanner_user_id,
                        credential.bot_id,
                        _credential_envelope(credential),
                        session.expires_at,
                    )
                    session.scanner_id = credential.scanner_user_id
                    session.bot_id = credential.bot_id
                    session.status = "confirmed"
                    session.challenge = None
            except WeixinProtocolError as exc:
                if not exc.retryable:
                    session.status = "failed"
                    session.challenge = None
            except Exception:
                session.status = "failed"
                session.challenge = None
                raise
        return self._pairing_result(session)

    async def submit_pairing_code(self, project_id: UUID, session_id: UUID, code: str) -> dict[str, Any]:
        async with self._pairing_lock:
            session = self._sessions.get(session_id)
            if session is None or session.project_id != project_id:
                raise NotFound("pairing session not found")
            if session.expires_at <= datetime.now(UTC) or session.status != "needs_code":
                raise Conflict("pairing is not waiting for a verification code")
            if not code or len(code) > 32 or any(char.isspace() or ord(char) < 33 for char in code):
                raise ValueError("invalid verification code")
            session.verify_code = code
            session.status = "scanned"
            return self._pairing_result(session)

    async def _expire_pairing(self, session: PairingSession) -> None:
        if session.challenge_id is None:
            return
        try:
            await asyncio.to_thread(
                self._guarded,
                self.core.expire_pairing,
                session.project_id,
                session.channel_id,
                session.challenge_id,
            )
        except (Conflict, NotFound):
            pass  # Local confirmation already consumed the staged token.
        session.challenge_id = None

    async def _prune_pairings(self) -> None:
        now = datetime.now(UTC)
        async with self._pairing_lock:
            for session_id, session in list(self._sessions.items()):
                if session.expires_at <= now:
                    await self._expire_pairing(session)
                    session.status = "expired"
                    session.challenge = None
                    session.verify_code = None
                    if session.expires_at + timedelta(minutes=5) <= now:
                        del self._sessions[session_id]

    @staticmethod
    def _pairing_result(session: PairingSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "status": session.status,
            "expires_at": session.expires_at,
            "channel_id": session.channel_id if session.challenge_id else None,
            "challenge_id": session.challenge_id,
            "scanner_id": session.scanner_id,
            "bot_id": session.bot_id,
        }
