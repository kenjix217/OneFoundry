"""Local operator setup routes for ephemeral WeChat QR pairing."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.remote_channel_runtime import RemoteChannelRuntime
from onefoundry.tool_gateway import Conflict, NotFound, Unavailable
from onefoundry.weixin_ilink import WeixinProtocolError
from onefoundry.workflow_schemas import StrictModel


class PairingStart(StrictModel):
    profile_id: UUID
    work_order_id: UUID | None = None  # Accepted from older clients; pairing is project scoped.
    reply_policy: Literal["bound_dm"] = "bound_dm"


class PairingStartOut(StrictModel):
    session_id: UUID
    qr_image_data_url: str
    expires_at: datetime


class PairingVerifyCode(StrictModel):
    code: SecretStr = Field(min_length=1, max_length=32)


class PairingPollOut(StrictModel):
    session_id: UUID
    status: Literal["waiting", "scanned", "needs_code", "expired", "failed", "confirmed"]
    expires_at: datetime
    channel_id: UUID | None = None
    challenge_id: UUID | None = None
    scanner_id: str | None = None
    bot_id: str | None = None


def make_remote_channel_runtime_router(runtime: RemoteChannelRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/channels/wechat/pairing")

    @router.post("/start", response_model=PairingStartOut)
    async def start(project_id: UUID, data: PairingStart) -> PairingStartOut:
        try:
            result = await runtime.start_pairing(project_id, data.profile_id, data.reply_policy)
        except WeixinProtocolError as exc:
            raise HTTPException(502, exc.code) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "pairing storage unavailable") from None
        return PairingStartOut.model_validate(result)

    @router.get("/{session_id}", response_model=PairingPollOut)
    async def poll(project_id: UUID, session_id: UUID) -> PairingPollOut:
        try:
            result = await runtime.poll_pairing(project_id, session_id)
        except WeixinProtocolError as exc:
            raise HTTPException(502, exc.code) from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except (Unavailable, SQLAlchemyError):
            raise HTTPException(503, "pairing storage unavailable") from None
        return PairingPollOut.model_validate(result)

    @router.post("/{session_id}/verify-code", response_model=PairingPollOut)
    async def verify_code(project_id: UUID, session_id: UUID, data: PairingVerifyCode) -> PairingPollOut:
        try:
            result = await runtime.submit_pairing_code(project_id, session_id, data.code.get_secret_value())
        except ValueError:
            raise HTTPException(422, "invalid verification code") from None
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None
        return PairingPollOut.model_validate(result)

    return router
