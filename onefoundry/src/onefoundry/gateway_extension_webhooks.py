"""Gateway-owned signed webhook setup, admission and durable delivery receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from onefoundry.coordination_store import record_signal
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.role_tool_admission import ToolOutputRejected, _reject_sensitive_output
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
EVENT_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")


class WebhookCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    signing_secret: str = Field(min_length=32, max_length=128, repr=False)
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("webhook name cannot be blank")
        return value.strip()

    @field_validator("signing_secret")
    @classmethod
    def valid_secret(cls, value: str) -> str:
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError("signing secret must contain printable non-space ASCII")
        return value


class WebhookPatch(StrictModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class WebhookCredentialPut(StrictModel):
    expected_version: int = Field(ge=1)
    value: str = Field(min_length=32, max_length=128, repr=False)

    @field_validator("value")
    @classmethod
    def valid_value(cls, value: str) -> str:
        return WebhookCreate.valid_secret(value)


class WebhookOut(StrictModel):
    id: UUID
    name: str
    enabled: bool
    version: int
    credential_present: bool
    created_at: datetime


class SignatureRejected(Exception):
    """The remote request has no valid subscription-bound signature."""


class PayloadRejected(Exception):
    """The signed payload cannot become a bounded coordination signal."""


def secret_store(settings: Settings) -> ModelSecretStore:
    ModelSecretStore(settings.model_secret_root).check_ready()
    root = Path(settings.model_secret_root) / "extension-webhooks"
    root.mkdir(mode=0o700, exist_ok=True)
    store = ModelSecretStore(str(root))
    store.check_ready()
    return store


def _public(row: dict[str, Any], store: ModelSecretStore) -> WebhookOut:
    return WebhookOut(
        id=row["id"],
        name=row["name"],
        enabled=row["enabled"],
        version=row["version"],
        credential_present=store.present(row["credential_id"]),
        created_at=row["created_at"],
    )


def _settings(gateway: ToolGateway) -> Settings:
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("webhook settings are unavailable")
    return settings


def list_webhooks(gateway: ToolGateway, project_id: UUID) -> list[WebhookOut]:
    store = secret_store(_settings(gateway))
    with gateway.engine.connect() as connection:
        gateway._require_project(connection, project_id)
        rows = (
            connection.execute(
                text("SELECT * FROM extension_webhooks WHERE project_id=:p ORDER BY created_at,id"),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
    return [_public(dict(row), store) for row in rows]


def create_webhook(gateway: ToolGateway, project_id: UUID, data: WebhookCreate) -> WebhookOut:
    store = secret_store(_settings(gateway))
    webhook_id, credential_id = uuid4(), uuid4()
    try:
        store.write(credential_id, data.signing_secret)
        with gateway.engine.begin() as connection:
            gateway._require_project(connection, project_id)
            row = (
                connection.execute(
                    text(
                        "INSERT INTO extension_webhooks "
                        "(id,project_id,actor_id,name,enabled,credential_id) "
                        "VALUES (:id,:p,:actor,:name,:enabled,:credential) RETURNING *"
                    ),
                    {
                        "id": webhook_id,
                        "p": project_id,
                        "actor": gateway.actor_id,
                        "name": data.name,
                        "enabled": data.enabled,
                        "credential": credential_id,
                    },
                )
                .mappings()
                .one()
            )
            gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_webhook_created",
                {"webhook_id": str(webhook_id), "enabled": data.enabled},
            )
        return _public(dict(row), store)
    except IntegrityError as exc:
        store.remove(credential_id)
        raise Conflict("webhook name already exists") from exc
    except Exception:
        store.remove(credential_id)
        raise


def patch_webhook(gateway: ToolGateway, project_id: UUID, webhook_id: UUID, data: WebhookPatch) -> WebhookOut:
    store = secret_store(_settings(gateway))
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        row = (
            connection.execute(
                text("SELECT * FROM extension_webhooks WHERE project_id=:p AND id=:id FOR UPDATE"),
                {"p": project_id, "id": webhook_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("webhook not found")
        if row["version"] != data.expected_version:
            raise Conflict("webhook changed; reload before saving")
        if data.enabled and not store.present(row["credential_id"]):
            raise Unavailable("webhook signing credential is unavailable")
        changed = (
            connection.execute(
                text(
                    "UPDATE extension_webhooks SET enabled=:enabled,version=version+1,"
                    "updated_at=clock_timestamp() WHERE project_id=:p AND id=:id RETURNING *"
                ),
                {"p": project_id, "id": webhook_id, "enabled": data.enabled},
            )
            .mappings()
            .one()
        )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "extension_webhook_updated",
            {"webhook_id": str(webhook_id), "enabled": data.enabled, "version": changed["version"]},
        )
    return _public(dict(changed), store)


def rotate_credential(
    gateway: ToolGateway, project_id: UUID, webhook_id: UUID, data: WebhookCredentialPut
) -> WebhookOut:
    WebhookCreate.valid_secret(data.value)
    store = secret_store(_settings(gateway))
    replacement = uuid4()
    prior: UUID | None = None
    try:
        store.write(replacement, data.value)
        with gateway.engine.begin() as connection:
            gateway._require_project(connection, project_id)
            row = (
                connection.execute(
                    text("SELECT * FROM extension_webhooks WHERE project_id=:p AND id=:id FOR UPDATE"),
                    {"p": project_id, "id": webhook_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("webhook not found")
            if row["version"] != data.expected_version:
                raise Conflict("webhook changed; reload before rotating")
            prior = row["credential_id"]
            changed = (
                connection.execute(
                    text(
                        "UPDATE extension_webhooks SET credential_id=:credential,version=version+1,"
                        "updated_at=clock_timestamp() WHERE project_id=:p AND id=:id RETURNING *"
                    ),
                    {"p": project_id, "id": webhook_id, "credential": replacement},
                )
                .mappings()
                .one()
            )
            gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_webhook_credential_rotated",
                {"webhook_id": str(webhook_id), "version": changed["version"]},
            )
    except Exception:
        store.remove(replacement)
        raise
    if prior is not None:
        store.remove(prior)
    return _public(dict(changed), store)


def _payload_fields(body: bytes, webhook_id: UUID, webhook_name: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise ValueError("nonfinite JSON value")

    try:
        value = json.loads(
            body,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PayloadRejected("webhook payload must be a JSON object") from exc
    if not isinstance(value, dict):
        raise PayloadRejected("webhook payload must be a JSON object")
    event_type = value.get("event_type", "generic")
    subject = value.get("subject", "")
    summary = value.get("summary", "")
    if (
        not isinstance(event_type, str)
        or not EVENT_TYPE.fullmatch(event_type)
        or not isinstance(subject, str)
        or len(subject) > 200
        or not isinstance(summary, str)
        or len(summary) > 500
    ):
        raise PayloadRejected("webhook payload fields are invalid")
    fields = {
        "webhook_id": str(webhook_id),
        "webhook_name": webhook_name,
        "event_type": event_type,
        "subject": subject,
        "summary": summary,
    }
    try:
        _reject_sensitive_output(fields)
    except ToolOutputRejected as exc:
        raise PayloadRejected("webhook payload contains protected content") from exc
    return fields


def receive_webhook(
    gateway: ToolGateway,
    webhook_id: UUID,
    *,
    timestamp: str | None,
    delivery_id: str | None,
    signature: str | None,
    body: bytes,
    now: int | None = None,
) -> dict[str, Any]:
    settings = _settings(gateway)
    if len(body) > settings.extension_webhook_max_bytes:
        raise PayloadRejected("webhook payload exceeds configured bound")
    if (
        timestamp is None
        or not timestamp.isascii()
        or not timestamp.isdecimal()
        or len(timestamp) > 12
        or delivery_id is None
        or not DELIVERY_ID.fullmatch(delivery_id)
        or signature is None
        or SIGNATURE.fullmatch(signature) is None
    ):
        raise SignatureRejected("webhook signature headers are invalid")
    current = int(time.time()) if now is None else now
    if abs(current - int(timestamp)) > settings.extension_webhook_window_seconds:
        raise SignatureRejected("webhook timestamp is outside the accepted window")
    store = secret_store(settings)
    with gateway.engine.begin() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM extension_webhooks WHERE id=:id FOR UPDATE"),
                {"id": webhook_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not row["enabled"]:
            raise NotFound("enabled webhook not found")
        try:
            secret = store.read(row["credential_id"])
        except SecretStoreError:
            raise Unavailable("webhook credential is unavailable") from None
        signed = timestamp.encode() + b"." + delivery_id.encode() + b"." + body
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
            raise SignatureRejected("webhook signature is invalid")
        digest = hashlib.sha256(body).hexdigest()
        prior = (
            connection.execute(
                text(
                    "SELECT payload_sha256,signal_id FROM extension_webhook_deliveries "
                    "WHERE project_id=:p AND webhook_id=:id AND delivery_id=:delivery"
                ),
                {"p": row["project_id"], "id": webhook_id, "delivery": delivery_id},
            )
            .mappings()
            .one_or_none()
        )
        if prior is not None:
            if prior["payload_sha256"] != digest:
                raise Conflict("delivery ID already used for different content")
            return {"status": "accepted", "duplicate": True, "signal_id": prior["signal_id"]}
        fields = _payload_fields(body, webhook_id, row["name"])
        actor_id = f"webhook:{webhook_id}"
        signal_id = record_signal(
            gateway,
            connection,
            row["project_id"],
            source="webhook",
            signal_type="webhook.received",
            dedupe_key=f"{webhook_id}:{delivery_id}",
            fields=fields,
            actor_id=actor_id,
        )
        if signal_id is None:
            raise Conflict("webhook signal identity already exists")
        connection.execute(
            text(
                "INSERT INTO extension_webhook_deliveries "
                "(id,project_id,webhook_id,actor_id,delivery_id,payload_sha256,signal_id) "
                "VALUES (:id,:p,:webhook,:actor,:delivery,:digest,:signal)"
            ),
            {
                "id": uuid4(),
                "p": row["project_id"],
                "webhook": webhook_id,
                "actor": actor_id,
                "delivery": delivery_id,
                "digest": digest,
                "signal": signal_id,
            },
        )
        gateway._event(
            connection,
            row["project_id"],
            None,
            None,
            "extension_webhook_received",
            {"webhook_id": str(webhook_id), "delivery_id": delivery_id, "signal_id": str(signal_id)},
            actor_id=actor_id,
        )
    return {"status": "accepted", "duplicate": False, "signal_id": signal_id}
