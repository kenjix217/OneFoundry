"""Local-only setup and redacted status for remote operator channels."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr

from onefoundry.model_secrets import SecretStoreError
from onefoundry.remote_channel_core import RemoteChannelCore
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel


class ChannelConfigure(StrictModel):
    sender_id: str = Field(min_length=1, max_length=2048)
    chat_id: str | None = Field(default=None, min_length=1, max_length=2048)
    profile_id: UUID
    work_order_id: UUID | None = None  # Older clients may send this; it no longer binds routing.
    reply_policy: Literal["approval_each", "bound_dm"] = "approval_each"
    enabled: bool = False
    bot_id: str | None = Field(default=None, max_length=2048)
    guild_id: str | None = Field(default=None, max_length=20)


class ChannelCredential(StrictModel):
    credential: SecretStr


class PairingConfirm(StrictModel):
    scanner_id: str = Field(min_length=1, max_length=2048)
    bot_id: str = Field(min_length=1, max_length=2048)


class IdentityConfirm(StrictModel):
    sender_id: str = Field(min_length=1, max_length=2048)
    chat_id: str = Field(min_length=1, max_length=2048)


class ChannelStatusOut(StrictModel):
    id: UUID
    project_id: UUID
    platform: Literal["discord", "wechat"]
    configured: bool
    enabled: bool
    reply_policy: Literal["approval_each", "bound_dm"]
    sender_id: str | None
    chat_id: str | None
    bot_id: str | None
    guild_id: str | None
    observed_sender_id: str | None
    observed_chat_id: str | None
    observed_at: datetime | None
    identity_confirmed: bool
    work_order_id: UUID | None
    profile_id: UUID | None
    credential_present: bool
    connected: bool
    last_heartbeat_at: datetime | None
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    last_error_code: str | None
    setup_readiness_reason: str | None


class PairingChallengeOut(StrictModel):
    id: UUID
    channel_id: UUID
    scanner_id: str
    bot_id: str
    expires_at: datetime


class OutboundStatusOut(StrictModel):
    id: UUID
    channel_id: UUID
    inbound_id: UUID
    status: Literal["approval_pending", "pending", "claimed", "accepted_by_platform", "failed", "unknown"]
    body_sha256: str
    remote_message_id: str | None
    reason: str | None
    created_at: datetime
    claimed_at: datetime | None
    finished_at: datetime | None


def make_remote_channel_router(core: RemoteChannelCore) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/channels")

    @router.get("", response_model=list[ChannelStatusOut])
    def list_channels(project_id: UUID) -> list[ChannelStatusOut]:
        rows = execute(lambda: core.status(project_id))
        assert isinstance(rows, list)
        return [ChannelStatusOut.model_validate(row) for row in rows]

    @router.get("/outbound", response_model=list[OutboundStatusOut])
    def outbound(project_id: UUID) -> list[OutboundStatusOut]:
        rows = execute(lambda: core.outbound_status(project_id))
        return [OutboundStatusOut.model_validate(row) for row in rows]

    @router.get("/pairing-challenges", response_model=list[PairingChallengeOut])
    def pairing_challenges(project_id: UUID) -> list[PairingChallengeOut]:
        rows = execute(lambda: core.pairing_challenges(project_id))
        return [PairingChallengeOut.model_validate(row) for row in rows]

    @router.get("/{channel_id}", response_model=ChannelStatusOut)
    def get_channel(project_id: UUID, channel_id: UUID) -> ChannelStatusOut:
        row = execute(lambda: core.status(project_id, channel_id))
        assert isinstance(row, dict)
        return ChannelStatusOut.model_validate(row)

    @router.put("/{platform}", response_model=ChannelStatusOut)
    def configure(
        project_id: UUID, platform: Literal["discord", "wechat"], data: ChannelConfigure
    ) -> ChannelStatusOut:
        try:
            row = execute(
                lambda: core.configure(
                    project_id,
                    platform,
                    sender_id=data.sender_id,
                    chat_id=data.chat_id,
                    work_order_id=data.work_order_id,
                    profile_id=data.profile_id,
                    reply_policy=data.reply_policy,
                    enabled=data.enabled,
                    bot_id=data.bot_id,
                    guild_id=data.guild_id,
                )
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return ChannelStatusOut.model_validate(row)

    @router.put("/{channel_id}/credential", response_model=ChannelStatusOut)
    def credential(project_id: UUID, channel_id: UUID, data: ChannelCredential) -> ChannelStatusOut:
        try:
            row = execute(
                lambda: core.put_credential(project_id, channel_id, data.credential.get_secret_value())
            )
        except SecretStoreError:
            raise HTTPException(503, "remote credential store unavailable") from None
        return ChannelStatusOut.model_validate(row)

    @router.post("/{channel_id}/pairing/{challenge_id}/confirm", response_model=ChannelStatusOut)
    def confirm_pairing(
        project_id: UUID, channel_id: UUID, challenge_id: UUID, data: PairingConfirm
    ) -> ChannelStatusOut:
        row = execute(
            lambda: core.confirm_pairing(project_id, channel_id, challenge_id, data.scanner_id, data.bot_id)
        )
        assert isinstance(row, dict)
        return ChannelStatusOut.model_validate(row)

    @router.post("/{channel_id}/identity/confirm", response_model=ChannelStatusOut)
    def confirm_identity(project_id: UUID, channel_id: UUID, data: IdentityConfirm) -> ChannelStatusOut:
        row = execute(lambda: core.confirm_identity(project_id, channel_id, data.sender_id, data.chat_id))
        assert isinstance(row, dict)
        return ChannelStatusOut.model_validate(row)

    return router
