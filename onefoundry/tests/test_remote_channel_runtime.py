"""Offline runtime tests with fake core and provider transports."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from onefoundry.discord_dm import DiscordInbound, DiscordSendReceipt, DiscordStatus
from onefoundry.remote_channel_core import RemoteChannelCore
from onefoundry.remote_channel_runtime import (
    ChannelSnapshot,
    RemoteChannelRuntime,
    _credential_envelope,
    _decode_credential,
    _qr_payload,
)
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict
from onefoundry.weixin_ilink import (
    InboundText,
    QRChallenge,
    QRPollResult,
    QRStatus,
    SendAcceptance,
    UpdateBatch,
    WeixinCredentials,
    WeixinProtocolError,
)


class FakeCore:
    def __init__(self, destination_id: str = "scanner@im.wechat") -> None:
        self.calls: list[tuple[str, Any]] = []
        self.channel_id = uuid4()
        self.claim_error = False
        self.destination_id = destination_id

    def status(
        self, project_id: UUID, channel_id: UUID | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        self.calls.append(("status", project_id))
        return []

    def validate_pairing_route(self, project_id: UUID, profile_id: UUID) -> None:
        self.calls.append(("validate", profile_id))

    def configure(self, project_id: UUID, platform: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("configure", kwargs))
        return {"id": self.channel_id}

    def stage_pairing(
        self,
        project_id: UUID,
        channel_id: UUID,
        scanner_id: str,
        bot_id: str,
        credential: str,
        expires_at: datetime,
    ) -> UUID:
        self.calls.append(("stage", (scanner_id, bot_id, credential)))
        return uuid4()

    def claim_outbound(self, project_id: UUID, outbound_id: UUID) -> dict[str, Any]:
        self.calls.append(("claim", outbound_id))
        if self.claim_error:
            raise Conflict("approval pending")
        return {"text": "reply", "destination_id": self.destination_id, "context_token": "context"}

    def pending_outbound_ids(self, project_id: UUID, channel_id: UUID) -> list[UUID]:
        return []

    def reject_pending_outbound(self, project_id: UUID, outbound_id: UUID, reason: str) -> None:
        self.calls.append(("reject", outbound_id))

    def finish_outbound(
        self,
        project_id: UUID,
        outbound_id: UUID,
        status: str,
        remote_message_id: str | None,
        reason: str | None,
    ) -> None:
        self.calls.append(("finish", (status, remote_message_id, reason)))


class FakeWeixin:
    def __init__(self, **_kwargs: Any) -> None:
        self.started = False

    async def start_qr(self) -> QRChallenge:
        self.started = True
        return QRChallenge(
            "private-token",
            "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=private-token&bot_type=3",
            "https://ilinkai.weixin.qq.com",
        )

    async def poll_qr(self, _challenge: QRChallenge, **_kwargs: Any) -> QRPollResult:
        return QRPollResult(
            QRStatus.CONFIRMED_UNBOUND,
            "https://ilinkai.weixin.qq.com",
            WeixinCredentials(
                "bot", "scanner@im.wechat", "private-credential", "https://ilinkai.weixin.qq.com"
            ),
        )

    async def aclose(self) -> None:
        return None


class FakeDiscord:
    def __init__(self, receipt: DiscordSendReceipt) -> None:
        self.receipt = receipt
        self.sent: list[str] = []

    async def send(self, text: str, expected_chat_id: str) -> DiscordSendReceipt:
        assert expected_chat_id == "555"
        self.sent.append(text)
        return self.receipt


def runtime(core: FakeCore) -> RemoteChannelRuntime:
    return RemoteChannelRuntime(
        cast(RemoteChannelCore, core),
        Settings(db_password="test-only", remote_channel_network_enabled=True),
        weixin_factory=cast(Any, FakeWeixin),
    )


def test_qr_payload_is_tencent_landing_url_bound_to_poll_token() -> None:
    challenge = QRChallenge(
        "secret",
        "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=secret&bot_type=3",
        "https://ilinkai.weixin.qq.com",
    )
    assert _qr_payload(challenge) == challenge.image_content
    for value in [
        "https://evil.example/q/7GiQu1?qrcode=secret&bot_type=3",
        "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=other&bot_type=3",
        "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=secret&bot_type=3&x=y",
    ]:
        with pytest.raises(WeixinProtocolError):
            _qr_payload(QRChallenge("secret", value, challenge.poll_base_url))


def test_qr_start_validates_local_route_before_provider_and_stages_bound_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        core = FakeCore()
        manager = runtime(core)
        captured: list[str] = []

        class FakeImage:
            def to_string(self) -> bytes:
                return b"<svg/>"

        def fake_make(value: str, **_kwargs: Any) -> FakeImage:
            captured.append(value)
            return FakeImage()

        monkeypatch.setattr("qrcode.make", fake_make)
        project_id, profile_id = uuid4(), uuid4()
        start = await manager.start_pairing(project_id, profile_id)
        assert [call[0] for call in core.calls] == ["status", "validate"]
        assert captured == ["https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=private-token&bot_type=3"]
        assert base64.b64decode(start["qr_image_data_url"].split(",", 1)[1]) == b"<svg/>"
        poll = await manager.poll_pairing(project_id, start["session_id"])
        assert poll["status"] == "confirmed"
        assert poll["channel_id"] == core.channel_id
        assert [call[0] for call in core.calls] == ["status", "validate", "configure", "stage"]
        staged = core.calls[-1][1]
        decoded = _decode_credential(staged[2])
        assert decoded.token == "private-credential"
        assert decoded.scanner_user_id == "scanner@im.wechat"
        assert "private-credential" not in str(poll)

    asyncio.run(scenario())


def test_claim_precedes_discord_send_and_ambiguous_receipt_is_unknown() -> None:
    async def scenario() -> None:
        core = FakeCore(destination_id="555")
        manager = runtime(core)
        project_id, channel_id, outbound_id = uuid4(), uuid4(), uuid4()
        row = ChannelSnapshot(
            project_id,
            channel_id,
            "discord",
            "42",
            "555",
            None,
            True,
            True,
            True,
            1,
            uuid4(),
            uuid4(),
        )
        client = FakeDiscord(DiscordSendReceipt("unknown", None, "delivery_unknown"))
        await manager._send_discord(row, cast(Any, client), outbound_id)
        assert [call[0] for call in core.calls] == ["claim", "finish"]
        assert client.sent == ["reply"]
        assert core.calls[-1][1] == ("unknown", None, "delivery_unknown")
        core.claim_error = True
        await manager._send_discord(row, cast(Any, client), outbound_id)
        assert client.sent == ["reply"]
        assert core.calls[-1][0] == "reject"

    asyncio.run(scenario())


def test_stored_credential_envelope_validates_tencent_host() -> None:
    valid = WeixinCredentials("bot", "scanner@im.wechat", "token", "https://ilinkai.weixin.qq.com")
    assert _decode_credential(_credential_envelope(valid)) == valid
    invalid = WeixinCredentials("bot", "scanner@im.wechat", "token", "https://evil.example")
    with pytest.raises(WeixinProtocolError):
        _decode_credential(_credential_envelope(invalid))


def test_network_disabled_blocks_listener_and_qr_before_provider_call() -> None:
    async def scenario() -> None:
        core = FakeCore()

        def forbidden_provider(**_kwargs: Any) -> FakeWeixin:
            raise AssertionError("provider must not be called")

        manager = RemoteChannelRuntime(
            cast(RemoteChannelCore, core),
            Settings(db_password="test-only"),
            weixin_factory=cast(Any, forbidden_provider),
        )
        row = ChannelSnapshot(
            uuid4(),
            uuid4(),
            "discord",
            "42",
            "555",
            None,
            False,
            False,
            True,
            1,
            uuid4(),
            uuid4(),
        )
        assert not manager._needed(row)
        with pytest.raises(Conflict, match="network is disabled"):
            await manager.start_pairing(row.project_id, uuid4())
        assert core.calls == []

    asyncio.run(scenario())


def test_discord_disabled_listener_observes_dm_without_running_chief() -> None:
    async def scenario() -> None:
        observed = asyncio.Event()

        class PairingCore(FakeCore):
            def read_credential(self, _project: UUID, _channel: UUID) -> str:
                return "bot-token"

            def status(
                self, _project: UUID, _channel: UUID | None = None
            ) -> list[dict[str, Any]] | dict[str, Any]:
                return {"enabled": False, "identity_confirmed": False}

            def observe_identity(self, *_args: Any) -> None:
                self.calls.append(("observe", None))
                observed.set()

            def heartbeat(self, _project: UUID, _channel: UUID, _connected: bool, _code: str | None) -> None:
                return None

        class PairingDiscord:
            def __init__(self, *, on_inbound: Any, **_kwargs: Any) -> None:
                self.on_inbound = on_inbound
                self.closed = asyncio.Event()

            async def start(self) -> None:
                await self.on_inbound(DiscordInbound("1", "42", "555", "pair"))
                await self.closed.wait()

            async def stop(self) -> None:
                self.closed.set()

            def status(self) -> DiscordStatus:
                return DiscordStatus(True, None)

        core = PairingCore()
        manager = RemoteChannelRuntime(
            cast(RemoteChannelCore, core),
            Settings(db_password="test-only"),
            discord_factory=cast(Any, PairingDiscord),
        )
        row = ChannelSnapshot(
            uuid4(),
            uuid4(),
            "discord",
            "42",
            None,
            None,
            False,
            False,
            True,
            1,
            uuid4(),
            uuid4(),
        )
        worker = asyncio.create_task(manager._run_discord(row))
        try:
            await asyncio.wait_for(observed.wait(), timeout=2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert [call[0] for call in core.calls] == ["observe"]

    asyncio.run(scenario())


def test_discord_server_listener_passes_exact_route_to_pairing_core() -> None:
    async def scenario() -> None:
        observed = asyncio.Event()
        factory_args: dict[str, Any] = {}

        class PairingCore(FakeCore):
            def read_credential(self, _project: UUID, _channel: UUID) -> str:
                return "bot-token"

            def status(self, _project: UUID, _channel: UUID | None = None) -> dict[str, Any]:
                return {"enabled": False, "identity_confirmed": False}

            def observe_identity(self, *args: Any) -> None:
                self.calls.append(("observe", args))
                observed.set()

            def heartbeat(self, _project: UUID, _channel: UUID, _connected: bool, _code: str | None) -> None:
                return None

        class PairingDiscord:
            def __init__(self, *, on_inbound: Any, **kwargs: Any) -> None:
                self.on_inbound = on_inbound
                self.closed = asyncio.Event()
                factory_args.update(kwargs)

            async def start(self) -> None:
                await self.on_inbound(DiscordInbound("1", "42", "555", "New work", "777"))
                await self.closed.wait()

            async def stop(self) -> None:
                self.closed.set()

            def status(self) -> DiscordStatus:
                return DiscordStatus(True, None)

        core = PairingCore()
        manager = RemoteChannelRuntime(
            cast(RemoteChannelCore, core),
            Settings(db_password="test-only"),
            discord_factory=cast(Any, PairingDiscord),
        )
        row = ChannelSnapshot(
            uuid4(), uuid4(), "discord", "42", "555", "999", False, False, True, 1, None, uuid4(), "777"
        )
        worker = asyncio.create_task(manager._run_discord(row))
        try:
            await asyncio.wait_for(observed.wait(), timeout=2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert factory_args["guild_id"] == "777"
        assert factory_args["channel_id"] == "555"
        assert factory_args["application_id"] == "999"
        assert core.calls == [("observe", (row.project_id, row.id, "42", "555", False, "777"))]

    asyncio.run(scenario())


def test_qr_verify_code_is_used_for_next_poll_only() -> None:
    async def scenario() -> None:
        core = FakeCore()

        class CodeClient(FakeWeixin):
            async def poll_qr(self, _challenge: QRChallenge, **kwargs: Any) -> QRPollResult:
                if kwargs.get("verify_code") != "123456":
                    return QRPollResult(QRStatus.NEED_VERIFYCODE, "https://ilinkai.weixin.qq.com")
                return await super().poll_qr(_challenge)

        manager = RemoteChannelRuntime(
            cast(RemoteChannelCore, core),
            Settings(db_password="test-only", remote_channel_network_enabled=True),
            weixin_factory=cast(Any, CodeClient),
        )
        project_id = uuid4()
        start = await manager.start_pairing(project_id, uuid4())
        assert (await manager.poll_pairing(project_id, start["session_id"]))["status"] == "needs_code"
        code_result = await manager.submit_pairing_code(project_id, start["session_id"], "123456")
        assert code_result["status"] == "scanned"
        confirmed = await manager.poll_pairing(project_id, start["session_id"])
        assert confirmed["status"] == "confirmed"
        assert manager._sessions[start["session_id"]].verify_code is None

    asyncio.run(scenario())


def test_preexisting_pending_discord_reply_dispatches_once_without_new_inbound() -> None:
    async def scenario() -> None:
        outbound_id = uuid4()
        second_scan = asyncio.Event()
        loop = asyncio.get_running_loop()

        class PendingCore(FakeCore):
            scans = 0
            pending = True

            def read_credential(self, _project: UUID, _channel: UUID) -> str:
                return "bot-token"

            def pending_outbound_ids(self, _project: UUID, _channel: UUID) -> list[UUID]:
                self.scans += 1
                if self.scans >= 2:
                    loop.call_soon_threadsafe(second_scan.set)
                return [outbound_id] if self.pending else []

            def heartbeat(self, _project: UUID, _channel: UUID, _connected: bool, _code: str | None) -> None:
                return None

            def finish_outbound(
                self,
                project_id: UUID,
                reply_id: UUID,
                status: str,
                remote_message_id: str | None,
                reason: str | None,
            ) -> None:
                super().finish_outbound(project_id, reply_id, status, remote_message_id, reason)
                self.pending = False

        class ConnectedDiscord:
            sent: list[str] = []

            def __init__(self, **_kwargs: Any) -> None:
                self.closed = asyncio.Event()

            async def start(self) -> None:
                await self.closed.wait()

            async def stop(self) -> None:
                self.closed.set()

            def status(self) -> DiscordStatus:
                return DiscordStatus(True, None)

            async def send(self, text: str, expected_chat_id: str) -> DiscordSendReceipt:
                assert expected_chat_id == "555"
                self.sent.append(text)
                return DiscordSendReceipt("accepted_by_platform", "remote-id", None)

        core = PendingCore(destination_id="555")
        manager = RemoteChannelRuntime(
            cast(RemoteChannelCore, core),
            Settings(db_password="test-only", remote_channel_poll_seconds=1),
            discord_factory=cast(Any, ConnectedDiscord),
        )
        row = ChannelSnapshot(
            uuid4(),
            uuid4(),
            "discord",
            "42",
            "555",
            None,
            True,
            True,
            True,
            1,
            uuid4(),
            uuid4(),
        )
        worker = asyncio.create_task(manager._run_discord(row))
        try:
            await asyncio.wait_for(second_scan.wait(), timeout=4)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert ConnectedDiscord.sent == ["reply"]
        assert [call[0] for call in core.calls] == ["claim", "finish"]

    asyncio.run(scenario())


@pytest.mark.parametrize("duplicate", [False, True])
def test_weixin_poll_filters_exact_identity_and_saves_cursor_after_durable_reply(
    duplicate: bool,
) -> None:
    async def scenario() -> None:
        project_id, channel_id, outbound_id = uuid4(), uuid4(), uuid4()
        calls: list[str] = []
        second_poll = asyncio.Event()

        class FlowCore(FakeCore):
            def read_credential(self, _project: UUID, _channel: UUID) -> str:
                return _credential_envelope(
                    WeixinCredentials("bot", "scanner@im.wechat", "token", "https://ilinkai.weixin.qq.com")
                )

            def load_cursor(self, _project: UUID, _channel: UUID) -> str:
                return "old"

            def receive(self, *_args: Any) -> dict[str, Any]:
                calls.append("receive")
                return {"outbound_id": None if duplicate else outbound_id, "duplicate": duplicate}

            def save_cursor(
                self, _project: UUID, _channel: UUID, before: str, after: str, accepted: list[str]
            ) -> None:
                assert (before, after, accepted) == ("old", "next", ["1"])
                calls.append("cursor")

            def heartbeat(self, _project: UUID, _channel: UUID, _connected: bool, _code: str | None) -> None:
                return None

        class FlowClient(FakeWeixin):
            count = 0

            async def get_updates(
                self, _credentials: WeixinCredentials, _cursor: str, **_kwargs: Any
            ) -> UpdateBatch:
                self.count += 1
                if self.count == 1:
                    return UpdateBatch(
                        (
                            InboundText("1", "scanner@im.wechat", "bot", "hello", "context"),
                            InboundText("2", "other@im.wechat", "bot", "ignored", "context"),
                            InboundText("3", "scanner@im.wechat", "other-bot", "ignored", "context"),
                        ),
                        "next",
                    )
                second_poll.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send_text(self, *_args: Any) -> SendAcceptance:
                calls.append("send")
                return SendAcceptance("accepted_by_platform", "client-id")

        core = FlowCore()
        manager = RemoteChannelRuntime(
            cast(RemoteChannelCore, core),
            Settings(db_password="test-only"),
            weixin_factory=cast(Any, FlowClient),
        )
        row = ChannelSnapshot(
            project_id,
            channel_id,
            "wechat",
            "scanner@im.wechat",
            "scanner@im.wechat",
            "bot",
            True,
            True,
            True,
            1,
            uuid4(),
            uuid4(),
        )
        worker = asyncio.create_task(manager._run_weixin(row))
        try:
            await asyncio.wait_for(second_poll.wait(), timeout=2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert calls == (["receive", "cursor"] if duplicate else ["receive", "send", "cursor"])
        assert [call[0] for call in core.calls] == ([] if duplicate else ["claim", "finish"])

    asyncio.run(scenario())
