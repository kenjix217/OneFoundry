"""Durable project-scoped remote message routing through the Tool Gateway."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.profile_hierarchy import lock_project
from onefoundry.provider_registry import provider_spec
from onefoundry.tool_gateway import Conflict, NotFound, Unavailable

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

Platform = Literal["discord", "wechat"]
ReplyPolicy = Literal["approval_each", "bound_dm"]
OutboundResult = Literal["accepted_by_platform", "failed", "unknown"]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_id(value: str, label: str) -> str:
    if not value or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise ValueError(f"invalid {label}")
    return value


class RemoteChannelCore:
    def __init__(self, gateway: ToolGateway, secret_root: str, identity_challenge_seconds: int) -> None:
        self.gateway = gateway
        self.secrets = ModelSecretStore(str(Path(secret_root) / "remote-channels"))
        self.identity_challenge_seconds = identity_challenge_seconds

    def prepare_secrets(self) -> None:
        ModelSecretStore(str(self.secrets.root.parent)).check_ready()
        try:
            self.secrets.root.mkdir(mode=0o700, exist_ok=True)
            self.secrets.check_ready()
        except (OSError, SecretStoreError):
            raise Unavailable("remote channel credential store unavailable") from None

    @contextmanager
    def _credential_lock(self, channel_id: UUID) -> Iterator[None]:
        self.secrets.check_ready()
        lock_path = self.secrets.root / f".{channel_id}.lock"
        fd = -1
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
            ):
                raise Unavailable("remote credential lock permissions invalid")
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd >= 0:
                os.close(fd)
            raise Unavailable("remote credential lock unavailable") from None
        except Unavailable:
            if fd >= 0:
                os.close(fd)
            raise
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _channel(self, db: Any, project_id: UUID, channel_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        statement = (
            "SELECT * FROM remote_channels WHERE project_id=:project_id AND id=:id FOR UPDATE"
            if lock
            else "SELECT * FROM remote_channels WHERE project_id=:project_id AND id=:id"
        )
        row = (
            db.execute(
                text(statement),
                {"project_id": project_id, "id": channel_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("remote channel not found")
        return dict(row)

    def _reject_channel_pending(self, db: Any, project_id: UUID, channel_id: UUID, reason: str) -> None:
        rows = (
            db.execute(
                text(
                    "UPDATE remote_outbound SET status='failed',reason=:reason,"
                    "finished_at=clock_timestamp() WHERE project_id=:project_id "
                    "AND channel_id=:channel_id AND status='pending' "
                    "RETURNING id,work_order_id"
                ),
                {"reason": reason, "project_id": project_id, "channel_id": channel_id},
            )
            .mappings()
            .all()
        )
        for row in rows:
            self.gateway._event(
                db,
                project_id,
                row["work_order_id"],
                None,
                "remote_reply_finished",
                {"outbound_id": str(row["id"]), "status": "failed", "reason": reason},
            )

    def _readiness(self, db: Any, channel: dict[str, Any], *, route_only: bool = False) -> str | None:
        if not route_only and (not channel["sender_id"] or not channel["chat_id"]):
            return "operator_identity_unbound"
        if not channel["profile_id"]:
            return "chief_profile_unbound"
        row = (
            db.execute(
                text(
                    "SELECT p.id AS profile_id,p.name,p.model,p.model_account_id,p.instructions,"
                    "a.id AS account_id,a.catalog_valid,a.credential_ready,a.auth_kind,a.provider "
                    "FROM profiles p "
                    "LEFT JOIN model_accounts a ON a.project_id=p.project_id AND a.id=p.model_account_id "
                    "WHERE p.project_id=:project_id AND p.id=:profile_id"
                ),
                {"project_id": channel["project_id"], "profile_id": channel["profile_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["name"]).lower() != "chief of staff":
            return "chief_profile_changed"
        if (
            not row["model"]
            or not row["instructions"]
            or not row["instructions"].strip()
            or not row["account_id"]
        ):
            return "chief_model_unconfigured"
        provider = provider_spec(str(row["provider"]))
        if provider is None or not provider.text_runtime_available:
            return "chief_model_unavailable"
        if not row["catalog_valid"] or not db.scalar(
            text(
                "SELECT 1 FROM model_catalog WHERE project_id=:project_id AND account_id=:account_id "
                "AND model_id=:model_id AND category='text'"
            ),
            {"project_id": channel["project_id"], "account_id": row["account_id"], "model_id": row["model"]},
        ):
            return "chief_model_unavailable"
        if row["auth_kind"] != "none" and not row["credential_ready"]:
            return "chief_model_credential_unavailable"
        if row["auth_kind"] != "none":
            settings = self.gateway.live_text_settings
            if settings is None:
                return "chief_model_credential_unavailable"
            try:
                if not ModelSecretStore(settings.model_secret_root).present(row["account_id"]):
                    return "chief_model_credential_unavailable"
            except SecretStoreError:
                return "chief_model_credential_unavailable"
        return None

    def validate_pairing_route(self, project_id: UUID, profile_id: UUID) -> None:
        with self.gateway.engine.connect() as db:
            self.gateway._require_project(db, project_id)
            self._require_chief_profile(db, project_id, profile_id)

    def _require_chief_profile(self, db: Any, project_id: UUID, profile_id: UUID | None) -> None:
        if (
            profile_id is None
            or db.scalar(
                text(
                    "SELECT 1 FROM profiles WHERE project_id=:p AND id=:profile "
                    "AND lower(name)='chief of staff'"
                ),
                {"p": project_id, "profile": profile_id},
            )
            is None
        ):
            raise Conflict("Chief of Staff profile is unavailable")

    def configure(
        self,
        project_id: UUID,
        platform: Platform,
        *,
        sender_id: str,
        chat_id: str | None,
        work_order_id: UUID | None,
        profile_id: UUID,
        reply_policy: ReplyPolicy = "approval_each",
        enabled: bool = False,
        bot_id: str | None = None,
        guild_id: str | None = None,
    ) -> dict[str, Any]:
        if platform not in {"discord", "wechat"} or reply_policy not in {"approval_each", "bound_dm"}:
            raise ValueError("invalid remote channel configuration")
        _bounded_id(sender_id, "sender ID")
        if chat_id is not None:
            _bounded_id(chat_id, "chat ID")
        if platform == "discord":
            if re.fullmatch(r"[1-9][0-9]{0,19}", sender_id) is None:
                raise ValueError("Discord sender ID must be a positive numeric snowflake")
            if chat_id is not None and re.fullmatch(r"[1-9][0-9]{0,19}", chat_id) is None:
                raise ValueError("Discord channel ID must be a positive numeric snowflake")
            if guild_id is not None:
                if re.fullmatch(r"[1-9][0-9]{0,19}", guild_id) is None:
                    raise ValueError("Discord server ID must be a positive numeric snowflake")
                if chat_id is None or bot_id is None or re.fullmatch(r"[1-9][0-9]{0,19}", bot_id) is None:
                    raise ValueError("Discord server mode needs a channel ID and application ID")
        elif not sender_id.endswith("@im.wechat") or chat_id not in {None, sender_id}:
            raise ValueError("WeChat sender and personal DM route must match")
        elif guild_id is not None:
            raise ValueError("WeChat does not use a Discord server ID")
        if bot_id is not None:
            _bounded_id(bot_id, "bot ID")
        # Accept the former work-order field from older local clients; channel routing is project-scoped.
        _ = work_order_id
        with self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            self._require_chief_profile(db, project_id, profile_id)
            old = (
                db.execute(
                    text(
                        "SELECT id,sender_id,chat_id,bot_id,guild_id,profile_id FROM remote_channels "
                        "WHERE project_id=:project_id AND platform=:platform FOR UPDATE"
                    ),
                    {"project_id": project_id, "platform": platform},
                )
                .mappings()
                .one_or_none()
            )
            channel_id = old["id"] if old else uuid4()
            if old is None:
                db.execute(
                    text(
                        "INSERT INTO remote_channels (id,project_id,actor_id,platform) "
                        "VALUES (:id,:project_id,:actor_id,:platform)"
                    ),
                    {
                        "id": channel_id,
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "platform": platform,
                    },
                )
            # A new identity or route must be explicitly re-enabled after review.
            changed = old is None or any(
                old[key] != value
                for key, value in {
                    "sender_id": sender_id,
                    "chat_id": chat_id,
                    "bot_id": bot_id,
                    "guild_id": guild_id,
                    "profile_id": profile_id,
                }.items()
            )
            channel = self._channel(db, project_id, channel_id)
            if platform == "wechat" and enabled and (changed or not channel["credential_ready"]):
                raise Conflict("WeChat pairing must be confirmed before enablement")
            if enabled and not channel["credential_ready"]:
                raise Conflict("remote channel credential is unavailable")
            if enabled and reply_policy != "bound_dm":
                raise Conflict("approval_each remote replies are not available")
            if enabled and (changed or not channel["identity_confirmed"]):
                raise Conflict("exact platform sender and chat must be locally confirmed")
            db.execute(
                text(
                    "UPDATE remote_channels SET sender_id=:sender_id,chat_id=:chat_id,"
                    "bot_id=:bot_id,guild_id=:guild_id,"
                    "work_order_id=NULL,profile_id=:profile_id,reply_policy=:reply_policy,"
                    "enabled=:enabled,connected=CASE WHEN :changed THEN false ELSE connected END,"
                    "identity_confirmed=CASE WHEN :changed THEN false ELSE identity_confirmed END,"
                    "observed_sender_id=CASE WHEN :changed THEN NULL ELSE observed_sender_id END,"
                    "observed_chat_id=CASE WHEN :changed THEN NULL ELSE observed_chat_id END,"
                    "observed_at=CASE WHEN :changed THEN NULL ELSE observed_at END "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "sender_id": sender_id,
                    "chat_id": chat_id,
                    "bot_id": bot_id,
                    "guild_id": guild_id,
                    "profile_id": profile_id,
                    "reply_policy": reply_policy,
                    "enabled": enabled,
                    "changed": changed,
                    "project_id": project_id,
                    "id": channel_id,
                },
            )
            if changed or not enabled:
                self._reject_channel_pending(db, project_id, channel_id, "channel_binding_changed")
            if enabled:
                reason = self._readiness(db, self._channel(db, project_id, channel_id))
                if reason:
                    raise Conflict(reason)
            self.gateway._event(
                db,
                project_id,
                None,
                None,
                "remote_channel_configured",
                {
                    "channel_id": str(channel_id),
                    "platform": platform,
                    "enabled": enabled,
                    "reply_policy": reply_policy,
                    "identity_changed": changed,
                },
            )
        return cast(dict[str, Any], self.status(project_id, channel_id))

    def put_credential(self, project_id: UUID, channel_id: UUID, credential: str) -> dict[str, Any]:
        self.prepare_secrets()
        with self._credential_lock(channel_id):
            with self.gateway.engine.begin() as db:
                lock_project(db, project_id)
                channel = self._channel(db, project_id, channel_id, lock=True)
                if channel["platform"] == "wechat":
                    raise Conflict("WeChat credential requires confirmed pairing")
                revision = int(channel["credential_revision"]) + 1
                db.execute(
                    text(
                        "UPDATE remote_channels SET credential_ready=false,"
                        "credential_revision=:revision,enabled=false,connected=false,"
                        "identity_confirmed=false,observed_sender_id=NULL,"
                        "observed_chat_id=NULL,observed_at=NULL "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"revision": revision, "project_id": project_id, "id": channel_id},
                )
                self._reject_channel_pending(db, project_id, channel_id, "credential_rotated")
                self.gateway._event(
                    db,
                    project_id,
                    channel["work_order_id"],
                    None,
                    "remote_channel_credential_rotation_started",
                    {"channel_id": str(channel_id), "credential_revision": revision},
                )
            self.secrets.write(channel_id, credential)
            with self.gateway.engine.begin() as db:
                lock_project(db, project_id)
                channel = self._channel(db, project_id, channel_id, lock=True)
                if channel["credential_revision"] != revision or channel["enabled"]:
                    raise Conflict("remote credential rotation changed")
                db.execute(
                    text(
                        "UPDATE remote_channels SET credential_ready=true "
                        "WHERE project_id=:project_id AND id=:id AND credential_revision=:revision"
                    ),
                    {"project_id": project_id, "id": channel_id, "revision": revision},
                )
                self.gateway._event(
                    db,
                    project_id,
                    channel["work_order_id"],
                    None,
                    "remote_channel_credential_changed",
                    {"channel_id": str(channel_id), "credential_revision": revision},
                )
        return cast(dict[str, Any], self.status(project_id, channel_id))

    def status(
        self, project_id: UUID, channel_id: UUID | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        with self.gateway.engine.connect() as db:
            self.gateway._require_project(db, project_id)
            rows = (
                [self._channel(db, project_id, channel_id)]
                if channel_id
                else [
                    dict(row)
                    for row in db.execute(
                        text("SELECT * FROM remote_channels WHERE project_id=:project_id ORDER BY platform"),
                        {"project_id": project_id},
                    ).mappings()
                ]
            )
            result = []
            for row in rows:
                reason = self._readiness(db, row)
                configured = bool(
                    row["sender_id"]
                    and row["chat_id"]
                    and row["profile_id"]
                    and (row["platform"] != "discord" or not row["guild_id"] or row["bot_id"])
                )
                try:
                    credential_present = bool(row["credential_ready"] and self.secrets.present(row["id"]))
                except SecretStoreError:
                    credential_present = False
                setup_reason = reason
                if setup_reason is None and not credential_present:
                    setup_reason = "channel_credential_unavailable"
                if setup_reason is None and not row["identity_confirmed"]:
                    setup_reason = "operator_identity_unconfirmed"
                result.append(
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "project_id",
                            "platform",
                            "enabled",
                            "reply_policy",
                            "sender_id",
                            "chat_id",
                            "bot_id",
                            "guild_id",
                            "work_order_id",
                            "profile_id",
                            "connected",
                            "last_heartbeat_at",
                            "last_inbound_at",
                            "last_outbound_at",
                            "last_error_code",
                        )
                    }
                    | {
                        "configured": configured,
                        "credential_present": credential_present,
                        "observed_sender_id": row["observed_sender_id"],
                        "observed_chat_id": row["observed_chat_id"],
                        "observed_at": row["observed_at"],
                        "identity_confirmed": row["identity_confirmed"],
                        "setup_readiness_reason": setup_reason,
                    }
                )
            return result[0] if channel_id else result

    def heartbeat(
        self, project_id: UUID, channel_id: UUID, connected: bool, error_code: str | None = None
    ) -> None:
        if error_code is not None and (
            len(error_code) > 80 or not error_code.isascii() or not error_code.replace("_", "").isalnum()
        ):
            raise ValueError("invalid error code")
        with self.gateway.engine.begin() as db:
            channel = self._channel(db, project_id, channel_id, lock=True)
            if connected and (not channel["enabled"] or not channel["identity_confirmed"]):
                raise Conflict("remote channel is not enabled and identity-confirmed")
            db.execute(
                text(
                    "UPDATE remote_channels SET connected=:connected,last_heartbeat_at=clock_timestamp(),"
                    "last_error_code=:error WHERE project_id=:project_id AND id=:id"
                ),
                {"connected": connected, "error": error_code, "project_id": project_id, "id": channel_id},
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_channel_heartbeat",
                {
                    "channel_id": str(channel_id),
                    "connected": connected,
                    "error_code": error_code,
                },
            )

    def observe_identity(
        self,
        project_id: UUID,
        channel_id: UUID,
        sender_id: str,
        chat_id: str,
        is_dm: bool,
        guild_id: str | None = None,
    ) -> dict[str, Any]:
        """Called only with an SDK-authenticated inbound platform event."""
        _bounded_id(sender_id, "sender ID")
        _bounded_id(chat_id, "chat ID")
        with self.gateway.engine.begin() as db:
            channel = self._channel(db, project_id, channel_id, lock=True)
            if is_dm != (channel["guild_id"] is None) or guild_id != channel["guild_id"]:
                raise Conflict("platform event is outside the configured Discord route")
            if channel["platform"] != "discord" or not channel["credential_ready"]:
                raise Conflict("Discord pairing is unavailable")
            if channel["enabled"]:
                raise Conflict("Discord channel is already enabled")
            if sender_id != channel["sender_id"]:
                raise Conflict("platform sender does not match configured operator")
            if channel["chat_id"] and channel["chat_id"] != chat_id:
                raise Conflict("observed Discord message does not match configured chat")
            db.execute(
                text(
                    "UPDATE remote_channels SET observed_sender_id=:sender_id,observed_chat_id=:chat_id,"
                    "observed_at=clock_timestamp(),identity_confirmed=false,enabled=false "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"sender_id": sender_id, "chat_id": chat_id, "project_id": project_id, "id": channel_id},
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_identity_observed",
                {"channel_id": str(channel_id)},
            )
        return cast(dict[str, Any], self.status(project_id, channel_id))

    def confirm_identity(
        self, project_id: UUID, channel_id: UUID, expected_sender_id: str, expected_chat_id: str
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as db:
            channel = self._channel(db, project_id, channel_id, lock=True)
            recent = db.scalar(
                text(
                    "SELECT observed_at > clock_timestamp() - make_interval(secs => :seconds) "
                    "FROM remote_channels WHERE project_id=:project_id AND id=:id"
                ),
                {"seconds": self.identity_challenge_seconds, "project_id": project_id, "id": channel_id},
            )
            if (
                channel["platform"] != "discord"
                or not recent
                or channel["observed_sender_id"] != expected_sender_id
                or channel["observed_chat_id"] != expected_chat_id
                or channel["sender_id"] != expected_sender_id
                or channel["chat_id"] not in {None, expected_chat_id}
            ):
                raise Conflict("observed Discord identity is missing, expired, or changed")
            db.execute(
                text(
                    "UPDATE remote_channels SET chat_id=:chat_id,identity_confirmed=true "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"chat_id": expected_chat_id, "project_id": project_id, "id": channel_id},
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_identity_confirmed",
                {"channel_id": str(channel_id)},
            )
        return cast(dict[str, Any], self.status(project_id, channel_id))

    def stage_pairing(
        self,
        project_id: UUID,
        channel_id: UUID,
        scanner_id: str,
        bot_id: str,
        credential: str,
        expires_at: datetime,
    ) -> UUID:
        self.prepare_secrets()
        _bounded_id(scanner_id, "scanner ID")
        _bounded_id(bot_id, "bot ID")
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise ValueError("pairing challenge expired")
        challenge_id = uuid4()
        with self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            channel = self._channel(db, project_id, channel_id, lock=True)
            if channel["platform"] != "wechat":
                raise Conflict("pairing is only available for WeChat")
            if channel["enabled"]:
                raise Conflict("disable WeChat before staging a new pairing")
            self.secrets.write(challenge_id, credential)
            db.execute(
                text(
                    "INSERT INTO remote_pairing_challenges "
                    "(id,project_id,actor_id,channel_id,scanner_id,bot_id,expires_at) "
                    "VALUES (:id,:project_id,:actor_id,:channel_id,:scanner_id,:bot_id,:expires_at)"
                ),
                {
                    "id": challenge_id,
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "channel_id": channel_id,
                    "scanner_id": scanner_id,
                    "bot_id": bot_id,
                    "expires_at": expires_at,
                },
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_pairing_staged",
                {"channel_id": str(channel_id), "challenge_id": str(challenge_id)},
            )
        return challenge_id

    def confirm_pairing(
        self,
        project_id: UUID,
        channel_id: UUID,
        challenge_id: UUID,
        expected_scanner_id: str,
        expected_bot_id: str,
    ) -> dict[str, Any]:
        with self._credential_lock(channel_id), self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            channel = self._channel(db, project_id, channel_id, lock=True)
            if channel["enabled"]:
                raise Conflict("disable WeChat before confirming a new pairing")
            challenge = (
                db.execute(
                    text(
                        "SELECT * FROM remote_pairing_challenges WHERE project_id=:project_id AND id=:id "
                        "AND channel_id=:channel_id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": challenge_id, "channel_id": channel_id},
                )
                .mappings()
                .one_or_none()
            )
            if channel["platform"] != "wechat" or challenge is None:
                raise NotFound("pairing challenge not found")
            if challenge["confirmed_at"] or challenge["expires_at"] <= datetime.now(UTC):
                raise Conflict("pairing challenge expired or used")
            if challenge["scanner_id"] != expected_scanner_id or challenge["bot_id"] != expected_bot_id:
                raise Conflict("pairing identity mismatch")
            if channel["sender_id"] != expected_scanner_id or channel["bot_id"] != expected_bot_id:
                raise Conflict("configured operator or bot identity mismatch")
            if channel["chat_id"] != expected_scanner_id:
                raise Conflict("WeChat personal DM destination does not match scanner identity")
            self._require_chief_profile(db, project_id, channel["profile_id"])
            try:
                credential = self.secrets.read(challenge_id)
                self.secrets.write(channel_id, credential)
            except SecretStoreError:
                raise Unavailable("pairing credential unavailable") from None
            db.execute(
                text(
                    "UPDATE remote_channels SET credential_ready=true,"
                    "credential_revision=credential_revision+1,"
                    "identity_confirmed=true,enabled=false,connected=false "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": channel_id},
            )
            db.execute(
                text(
                    "UPDATE remote_pairing_challenges SET confirmed_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": challenge_id},
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_pairing_confirmed",
                {"channel_id": str(channel_id), "challenge_id": str(challenge_id)},
            )
        self.secrets.remove(challenge_id)
        return cast(dict[str, Any], self.status(project_id, channel_id))

    def expire_pairing(self, project_id: UUID, channel_id: UUID, challenge_id: UUID) -> None:
        with self.gateway.engine.begin() as db:
            channel = self._channel(db, project_id, channel_id, lock=True)
            row = (
                db.execute(
                    text(
                        "SELECT confirmed_at FROM remote_pairing_challenges "
                        "WHERE project_id=:project_id AND channel_id=:channel_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "channel_id": channel_id, "id": challenge_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("pairing challenge not found")
            if row["confirmed_at"] is not None:
                raise Conflict("confirmed pairing cannot be expired")
            db.execute(
                text(
                    "UPDATE remote_pairing_challenges SET expires_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": challenge_id},
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_pairing_expired",
                {"channel_id": str(channel_id), "challenge_id": str(challenge_id)},
            )
        try:
            self.secrets.remove(challenge_id)
        except SecretStoreError:
            raise Unavailable("expired pairing credential cleanup failed") from None

    def load_cursor(self, project_id: UUID, channel_id: UUID) -> str:
        with self.gateway.engine.connect() as db:
            channel = self._channel(db, project_id, channel_id)
            if channel["platform"] != "wechat":
                raise Conflict("poll cursor is only available for WeChat")
            return str(channel["poll_cursor"])

    def save_cursor(
        self,
        project_id: UUID,
        channel_id: UUID,
        expected_cursor: str,
        new_cursor: str,
        accepted_message_ids: list[str],
    ) -> None:
        settings = self.gateway.live_text_settings
        if settings is None:
            raise Unavailable("WeChat cursor configuration unavailable")
        if (
            len(expected_cursor) > settings.weixin_max_cursor_length
            or len(new_cursor) > settings.weixin_max_cursor_length
        ):
            raise ValueError("poll cursor exceeds limit")
        if len(accepted_message_ids) > 1000 or len(set(accepted_message_ids)) != len(accepted_message_ids):
            raise ValueError("invalid cursor batch")
        for message_id in accepted_message_ids:
            _bounded_id(message_id, "platform message ID")
        with self.gateway.engine.begin() as db:
            channel = self._channel(db, project_id, channel_id, lock=True)
            if channel["platform"] != "wechat" or channel["poll_cursor"] != expected_cursor:
                raise Conflict("poll cursor changed")
            for message_id in accepted_message_ids:
                accepted = db.scalar(
                    text(
                        "SELECT 1 FROM remote_inbound WHERE project_id=:project_id "
                        "AND channel_id=:channel_id "
                        "AND platform_message_id=:message_id AND status IN ('done','failed','unknown')"
                    ),
                    {"project_id": project_id, "channel_id": channel_id, "message_id": message_id},
                )
                if accepted is None:
                    raise Conflict("poll batch message was not durably handled")
            db.execute(
                text(
                    "UPDATE remote_channels SET poll_cursor=:cursor WHERE project_id=:project_id AND id=:id"
                ),
                {"cursor": new_cursor, "project_id": project_id, "id": channel_id},
            )
            self.gateway._event(
                db,
                project_id,
                channel["work_order_id"],
                None,
                "remote_poll_cursor_saved",
                {"channel_id": str(channel_id), "cursor_sha256": _digest(new_cursor)},
            )

    def receive(
        self,
        project_id: UUID,
        channel_id: UUID,
        platform_message_id: str,
        sender_id: str,
        chat_id: str,
        message: str,
        is_dm: bool,
        context_token: str | None = None,
        guild_id: str | None = None,
    ) -> dict[str, Any]:
        _bounded_id(platform_message_id, "platform message ID")
        _bounded_id(sender_id, "sender ID")
        _bounded_id(chat_id, "chat ID")
        if context_token is not None:
            _bounded_id(context_token, "context token")
        if not message or len(message.encode()) > 1_000_000:
            raise ValueError("invalid remote message")
        digest = _digest(message)
        inbound_id = uuid4()
        client_turn_id = uuid5(channel_id, platform_message_id)
        with self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            channel = self._channel(db, project_id, channel_id, lock=True)
            if guild_id != channel["guild_id"] or is_dm != (guild_id is None):
                raise Conflict("remote message is outside the configured chat route")
            prior = (
                db.execute(
                    text(
                        "SELECT id,status,body_sha256,sender_id,chat_id FROM remote_inbound "
                        "WHERE project_id=:project_id "
                        "AND channel_id=:channel_id AND platform_message_id=:message_id"
                    ),
                    {"project_id": project_id, "channel_id": channel_id, "message_id": platform_message_id},
                )
                .mappings()
                .one_or_none()
            )
            if prior:
                if (
                    prior["body_sha256"] != digest
                    or prior["sender_id"] != sender_id
                    or prior["chat_id"] != chat_id
                ):
                    raise Conflict("platform message ID reused with different content or identity")
                return {"inbound_id": prior["id"], "status": prior["status"], "duplicate": True}
            if not channel["enabled"] or not channel["credential_ready"] or not channel["identity_confirmed"]:
                raise Conflict("remote channel is disabled or identity is unconfirmed")
            if channel["sender_id"] != sender_id or channel["chat_id"] != chat_id:
                raise Conflict("remote sender is not the bound operator")
            reason = self._readiness(db, channel)
            if reason:
                raise Conflict(reason)
            db.execute(
                text(
                    "INSERT INTO remote_inbound (id,project_id,actor_id,channel_id,platform_message_id,"
                    "sender_id,chat_id,body_sha256,work_order_id,credential_revision,"
                    "client_turn_id,status) VALUES "
                    "(:id,:project_id,:actor_id,:channel_id,:message_id,:sender_id,:chat_id,:digest,NULL,:revision,:turn_id,'processing')"
                ),
                {
                    "id": inbound_id,
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "channel_id": channel_id,
                    "message_id": platform_message_id,
                    "sender_id": sender_id,
                    "chat_id": chat_id,
                    "digest": digest,
                    "revision": channel["credential_revision"],
                    "turn_id": client_turn_id,
                },
            )
            db.execute(
                text(
                    "UPDATE remote_channels SET last_inbound_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": channel_id},
            )
            self.gateway._event(
                db,
                project_id,
                None,
                None,
                "remote_message_received",
                {"channel_id": str(channel_id), "inbound_id": str(inbound_id), "body_sha256": digest},
            )
        if context_token:
            try:
                self.secrets.write(inbound_id, context_token)
            except SecretStoreError:
                self._finish_inbound(project_id, inbound_id, "unknown", "context_token_unavailable")
                raise Unavailable("remote context token unavailable") from None
        try:
            self.gateway.enqueue_project_chief_turn(
                project_id,
                client_turn_id,
                message,
                source=channel["platform"],
                remote_inbound_id=inbound_id,
            )
        except Exception:
            self._finish_inbound(project_id, inbound_id, "unknown", "inbox_enqueue_unknown")
            return {"inbound_id": inbound_id, "status": "unknown", "duplicate": False}
        try:
            self.process_one_project_message()
        except Exception:
            # The queued turn remains durable for the inbox worker to reconcile.
            return {"inbound_id": inbound_id, "status": "processing", "duplicate": False}
        with self.gateway.engine.connect() as db:
            receipt = (
                db.execute(
                    text(
                        "SELECT i.status,o.id AS outbound_id FROM remote_inbound i "
                        "LEFT JOIN remote_outbound o ON o.project_id=i.project_id AND o.inbound_id=i.id "
                        "WHERE i.project_id=:p AND i.id=:id"
                    ),
                    {"p": project_id, "id": inbound_id},
                )
                .mappings()
                .one()
            )
        return {
            "inbound_id": inbound_id,
            "status": receipt["status"],
            "outbound_id": receipt["outbound_id"],
            "duplicate": False,
        }

    def process_one_project_message(self) -> bool:
        """Advance one durable Chief turn, then prepare any completed remote reply."""
        turn = self.gateway.process_project_chief_turn()
        if turn is not None and turn["remote_inbound_id"] is not None:
            self._finalize_project_message(turn["project_id"], turn["id"])
            return True
        with self.gateway.engine.connect() as db:
            pending = (
                db.execute(
                    text(
                        "SELECT t.project_id,t.id FROM project_chief_turns t JOIN remote_inbound i "
                        "ON i.project_id=t.project_id AND i.id=t.remote_inbound_id "
                        "WHERE i.status='processing' AND t.status IN ('done','failed','unknown','blocked') "
                        "ORDER BY t.created_at,t.id LIMIT 1"
                    )
                )
                .mappings()
                .one_or_none()
            )
        if pending is None:
            return turn is not None
        self._finalize_project_message(pending["project_id"], pending["id"])
        return True

    def _finalize_project_message(self, project_id: UUID, turn_id: UUID) -> None:
        with self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            row = (
                db.execute(
                    text(
                        "SELECT t.id AS turn_id,t.status AS turn_status,t.assistant_text,t.reason,"
                        "t.target_work_order_id,i.id AS inbound_id,i.status AS inbound_status,"
                        "i.sender_id,i.chat_id,i.credential_revision,c.id AS channel_id,c.enabled,"
                        "c.identity_confirmed,c.credential_ready,c.sender_id AS current_sender,"
                        "c.chat_id AS current_chat,c.credential_revision AS current_revision,"
                        "c.reply_policy,c.profile_id,t.profile_id AS turn_profile "
                        "FROM project_chief_turns t JOIN remote_inbound i "
                        "ON i.project_id=t.project_id AND i.id=t.remote_inbound_id "
                        "JOIN remote_channels c ON c.project_id=i.project_id AND c.id=i.channel_id "
                        "WHERE t.project_id=:p AND t.id=:turn FOR UPDATE OF i,c"
                    ),
                    {"p": project_id, "turn": turn_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["inbound_status"] != "processing":
                return
            if row["turn_status"] not in {"done", "failed", "unknown", "blocked"}:
                return
            current = (
                row["enabled"]
                and row["identity_confirmed"]
                and row["credential_ready"]
                and row["sender_id"] == row["current_sender"]
                and row["chat_id"] == row["current_chat"]
                and row["credential_revision"] == row["current_revision"]
                and row["profile_id"] == row["turn_profile"]
            )
            if not current:
                status, reason = "unknown", "channel_binding_changed"
                body = None
            elif row["turn_status"] == "done" and row["assistant_text"]:
                status, reason = "done", None
                body = str(row["assistant_text"])
            elif row["turn_status"] == "blocked":
                status, reason = "failed", "chief_unavailable"
                body = (
                    "I received your message, but the Chief of Staff is unavailable. "
                    "Check Messaging in OneFoundry."
                )
            elif row["turn_status"] == "failed":
                status, reason = "failed", "model_rejected"
                body = (
                    "I received your message, but the Chief of Staff could not respond. "
                    "Check Messaging in OneFoundry."
                )
            else:
                status, reason = "unknown", "model_outcome_unknown"
                body = (
                    "I received your message, but its outcome is uncertain. "
                    "Check Messaging before repeating it."
                )
            outbound_id: UUID | None = None
            if body is not None:
                outbound_id = uuid4()
                db.execute(
                    text(
                        "INSERT INTO remote_outbound (id,project_id,actor_id,channel_id,inbound_id,"
                        "work_order_id,chief_turn_id,destination_id,sender_id,reply_policy,"
                        "credential_revision,body,body_sha256,status) VALUES "
                        "(:id,:p,:actor,:channel,:inbound,:work,:turn,:destination,:sender,"
                        ":policy,:revision,:body,:digest,'pending')"
                    ),
                    {
                        "id": outbound_id,
                        "p": project_id,
                        "actor": self.gateway.actor_id,
                        "channel": row["channel_id"],
                        "inbound": row["inbound_id"],
                        "work": row["target_work_order_id"],
                        "turn": turn_id,
                        "destination": row["chat_id"],
                        "sender": row["sender_id"],
                        "policy": row["reply_policy"],
                        "revision": row["credential_revision"],
                        "body": body,
                        "digest": _digest(body),
                    },
                )
            db.execute(
                text(
                    "UPDATE remote_inbound SET status=:status,reason=:reason,"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                ),
                {"status": status, "reason": reason, "p": project_id, "id": row["inbound_id"]},
            )
            self.gateway._event(
                db,
                project_id,
                row["target_work_order_id"],
                None,
                "remote_inbound_finished",
                {
                    "inbound_id": str(row["inbound_id"]),
                    "status": status,
                    "reason": reason,
                    "chief_turn_id": str(turn_id),
                    "outbound_id": str(outbound_id) if outbound_id else None,
                },
            )

    def read_credential(self, project_id: UUID, channel_id: UUID) -> str:
        with self.gateway.engine.connect() as db:
            channel = self._channel(db, project_id, channel_id)
            if not channel["credential_ready"]:
                raise Conflict("remote channel credential is unavailable")
            if channel["platform"] == "discord" and not channel["sender_id"]:
                raise Conflict("Discord operator identity is not configured")
        try:
            return self.secrets.read(channel_id)
        except SecretStoreError:
            raise Unavailable("remote channel credential unavailable") from None

    def pairing_challenges(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as db:
            self.gateway._require_project(db, project_id)
            return [
                dict(row)
                for row in db.execute(
                    text(
                        "SELECT id,channel_id,scanner_id,bot_id,expires_at FROM remote_pairing_challenges "
                        "WHERE project_id=:project_id AND confirmed_at IS NULL "
                        "AND expires_at>clock_timestamp() "
                        "ORDER BY expires_at DESC LIMIT 20"
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]

    def outbound_status(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as db:
            self.gateway._require_project(db, project_id)
            return [
                dict(row)
                for row in db.execute(
                    text(
                        "SELECT id,channel_id,inbound_id,status,body_sha256,remote_message_id,reason,"
                        "created_at,claimed_at,finished_at FROM remote_outbound "
                        "WHERE project_id=:project_id ORDER BY created_at DESC,id DESC LIMIT 100"
                    ),
                    {"project_id": project_id},
                ).mappings()
            ]

    def pending_outbound_ids(self, project_id: UUID, channel_id: UUID) -> list[UUID]:
        """Recover durable replies that were prepared before process interruption."""
        with self.gateway.engine.connect() as db:
            self._channel(db, project_id, channel_id)
            return list(
                db.execute(
                    text(
                        "SELECT o.id FROM remote_outbound o JOIN remote_channels c "
                        "ON c.project_id=o.project_id AND c.id=o.channel_id "
                        "WHERE o.project_id=:project_id AND o.channel_id=:channel_id "
                        "AND o.status='pending' AND o.reply_policy='bound_dm' "
                        "AND c.enabled=true AND c.identity_confirmed=true AND c.credential_ready=true "
                        "AND o.sender_id=c.sender_id AND o.destination_id=c.chat_id "
                        "AND o.credential_revision=c.credential_revision "
                        "AND o.chief_turn_id IS NOT NULL AND o.reply_policy=c.reply_policy "
                        "ORDER BY o.created_at,o.id LIMIT 100"
                    ),
                    {"project_id": project_id, "channel_id": channel_id},
                ).scalars()
            )

    def reject_pending_outbound(self, project_id: UUID, outbound_id: UUID, reason: str) -> bool:
        """Terminalize an unsent intent after a deterministic binding failure."""
        if len(reason) > 80 or not reason.isascii() or not reason.replace("_", "").isalnum():
            raise ValueError("invalid remote rejection reason code")
        with self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            row = (
                db.execute(
                    text(
                        "SELECT inbound_id,work_order_id,status,reply_policy FROM remote_outbound "
                        "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": outbound_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("remote reply not found")
            if row["status"] != "pending" or row["reply_policy"] != "bound_dm":
                return False
            db.execute(
                text(
                    "UPDATE remote_outbound SET status='failed',reason=:reason,"
                    "finished_at=clock_timestamp() WHERE project_id=:project_id AND id=:id"
                ),
                {"reason": reason, "project_id": project_id, "id": outbound_id},
            )
            self.gateway._event(
                db,
                project_id,
                row["work_order_id"],
                None,
                "remote_reply_finished",
                {"outbound_id": str(outbound_id), "status": "failed", "reason": reason},
            )
        try:
            self.secrets.remove(row["inbound_id"])
        except (OSError, SecretStoreError):
            raise Unavailable("remote reply context cleanup failed") from None
        return True

    def _finish_inbound(self, project_id: UUID, inbound_id: UUID, status: str, reason: str) -> None:
        with self.gateway.engine.begin() as db:
            row = (
                db.execute(
                    text(
                        "SELECT work_order_id FROM remote_inbound "
                        "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": inbound_id},
                )
                .mappings()
                .one()
            )
            db.execute(
                text(
                    "UPDATE remote_inbound SET status=:status,reason=:reason,finished_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id AND status='processing'"
                ),
                {"status": status, "reason": reason, "project_id": project_id, "id": inbound_id},
            )
            self.gateway._event(
                db,
                project_id,
                row["work_order_id"],
                None,
                "remote_inbound_finished",
                {"inbound_id": str(inbound_id), "status": status, "reason": reason},
            )

    def claim_outbound(self, project_id: UUID, outbound_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.begin() as db:
            lock_project(db, project_id)
            row = (
                db.execute(
                    text(
                        "SELECT o.*,c.enabled,c.chat_id,c.credential_ready,c.platform,c.profile_id,"
                        "c.sender_id AS current_sender_id,c.reply_policy AS current_reply_policy,"
                        "c.credential_revision AS current_credential_revision,"
                        "o.chief_turn_id "
                        "FROM remote_outbound o JOIN remote_channels c "
                        "ON c.project_id=o.project_id AND c.id=o.channel_id "
                        "WHERE o.project_id=:project_id AND o.id=:id FOR UPDATE OF o,c"
                    ),
                    {"project_id": project_id, "id": outbound_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("remote reply not found")
            if row["status"] not in {"pending", "approval_pending"}:
                raise Conflict("remote reply already claimed or finished")
            if (
                not row["enabled"]
                or not row["credential_ready"]
                or row["chat_id"] != row["destination_id"]
                or row["current_sender_id"] != row["sender_id"]
                or row["current_reply_policy"] != row["reply_policy"]
                or row["current_credential_revision"] != row["credential_revision"]
                or row["chief_turn_id"] is None
            ):
                raise Conflict("remote channel binding changed")
            self._require_chief_profile(db, project_id, row["profile_id"])
            if row["reply_policy"] != "bound_dm" or row["approval_id"] is not None:
                raise Conflict("remote reply policy does not grant this send")
            context_token = None
            if row["platform"] == "wechat":
                try:
                    context_token = self.secrets.read(row["inbound_id"])
                except SecretStoreError:
                    raise Unavailable("WeChat reply context unavailable") from None
            db.execute(
                text(
                    "UPDATE remote_outbound SET status='claimed',claimed_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": outbound_id},
            )
            self.gateway._event(
                db,
                project_id,
                row["work_order_id"],
                None,
                "remote_reply_claimed",
                {"outbound_id": str(outbound_id), "body_sha256": row["body_sha256"]},
            )
        return {
            "outbound_id": outbound_id,
            "text": row["body"],
            "destination_id": row["destination_id"],
            "context_token": context_token,
        }

    def finish_outbound(
        self,
        project_id: UUID,
        outbound_id: UUID,
        status: OutboundResult,
        remote_message_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        if status not in {"accepted_by_platform", "failed", "unknown"}:
            raise ValueError("invalid remote send result")
        if remote_message_id is not None:
            _bounded_id(remote_message_id, "remote message ID")
        if reason is not None and (
            len(reason) > 80 or not reason.isascii() or not reason.replace("_", "").isalnum()
        ):
            raise ValueError("invalid remote result reason code")
        with self.gateway.engine.begin() as db:
            row = (
                db.execute(
                    text(
                        "SELECT work_order_id,chief_turn_id,channel_id,inbound_id,status "
                        "FROM remote_outbound "
                        "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": outbound_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("remote reply not found")
            if row["status"] != "claimed":
                raise Conflict("remote reply is not claimed")
            db.execute(
                text(
                    "UPDATE remote_outbound SET status=:status,"
                    "remote_message_id=:remote_message_id,reason=:reason,"
                    "finished_at=clock_timestamp() WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "status": status,
                    "remote_message_id": remote_message_id,
                    "reason": reason,
                    "project_id": project_id,
                    "id": outbound_id,
                },
            )
            db.execute(
                text(
                    "UPDATE remote_channels SET last_outbound_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": row["channel_id"]},
            )
            self.gateway._event(
                db,
                project_id,
                row["work_order_id"],
                None,
                "remote_reply_finished",
                {"outbound_id": str(outbound_id), "status": status, "reason": reason},
            )
            if row["work_order_id"] is not None and row["chief_turn_id"] is None:
                self.gateway.reconcile_external_action(
                    db,
                    project_id,
                    row["work_order_id"],
                    None,
                    confirmed=status == "accepted_by_platform" and remote_message_id is not None,
                )
        self.secrets.remove(row["inbound_id"])

    def reconcile_interrupted(self) -> None:
        with self.gateway.engine.begin() as db:
            inbound = (
                db.execute(
                    text(
                        "UPDATE remote_inbound SET status='unknown',reason='process_interrupted',"
                        "finished_at=clock_timestamp() WHERE status='processing' "
                        "AND NOT EXISTS (SELECT 1 FROM project_chief_turns t "
                        "WHERE t.project_id=remote_inbound.project_id "
                        "AND t.remote_inbound_id=remote_inbound.id) "
                        "RETURNING id,project_id,work_order_id"
                    )
                )
                .mappings()
                .all()
            )
            outbound = (
                db.execute(
                    text(
                        "UPDATE remote_outbound SET status='unknown',reason='process_interrupted',"
                        "finished_at=clock_timestamp() WHERE status='claimed' "
                        "RETURNING id,project_id,work_order_id"
                    )
                )
                .mappings()
                .all()
            )
            legacy_pending = (
                db.execute(
                    text(
                        "UPDATE remote_outbound SET status='failed',reason='project_inbox_migration',"
                        "finished_at=clock_timestamp() WHERE status IN ('pending','approval_pending') "
                        "AND chief_turn_id IS NULL "
                        "RETURNING id,project_id,work_order_id"
                    )
                )
                .mappings()
                .all()
            )
            for row in [*inbound, *outbound, *legacy_pending]:
                self.gateway._event(
                    db,
                    row["project_id"],
                    row["work_order_id"],
                    None,
                    "remote_message_reconciled",
                    {"record_id": str(row["id"]), "status": "unknown"},
                )
        self.cleanup_orphan_secrets()

    def cleanup_orphan_secrets(self) -> None:
        """Retain only committed channel, live pairing, and pending reply secrets."""
        with self.gateway.engine.connect() as db:
            active: set[UUID] = set(
                db.execute(text("SELECT id FROM remote_channels WHERE credential_ready=true")).scalars()
            )
            active.update(
                db.execute(
                    text(
                        "SELECT id FROM remote_pairing_challenges "
                        "WHERE confirmed_at IS NULL AND expires_at>clock_timestamp()"
                    )
                ).scalars()
            )
            active.update(
                db.execute(text("SELECT id FROM remote_inbound WHERE status='processing'")).scalars()
            )
            active.update(
                db.execute(
                    text(
                        "SELECT inbound_id FROM remote_outbound "
                        "WHERE status IN ('approval_pending','pending','claimed')"
                    )
                ).scalars()
            )
        try:
            self.secrets.remove_orphans(active)
        except SecretStoreError:
            raise Unavailable("remote channel credential cleanup failed") from None
