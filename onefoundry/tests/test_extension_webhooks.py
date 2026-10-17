"""Bounded signed-webhook payload and secret boundary tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.gateway_extension_webhooks import (
    PayloadRejected,
    WebhookCreate,
    WebhookCredentialPut,
    _payload_fields,
)


def test_webhook_credentials_validate_without_echoing_value() -> None:
    secret = "synthetic-secret-value-0123456789-abcdef"
    created = WebhookCreate(name="  Synthetic  ", signing_secret=secret)
    assert created.name == "Synthetic" and created.enabled is False
    assert secret not in repr(created)
    for value in ("short", "contains space and is otherwise long enough", "\n" * 32):
        with pytest.raises(ValidationError):
            WebhookCredentialPut(expected_version=1, value=value)


def test_webhook_payload_discards_unrecognized_fields_and_rejects_secrets() -> None:
    webhook_id = uuid4()
    fields = _payload_fields(
        b'{"event_type":"lead.created","subject":"Example","ignored":{"secret":"hidden"}}',
        webhook_id,
        "Fixture",
    )
    assert fields == {
        "webhook_id": str(webhook_id),
        "webhook_name": "Fixture",
        "event_type": "lead.created",
        "subject": "Example",
        "summary": "",
    }
    for body in (
        b"not-json",
        b"[]",
        b'{"event_type":"bad event"}',
        b'{"event_type":"lead.created","summary":"api_key = abcdefghijklmnopqrstuvwxyz"}',
        b'{"event_type":"lead.created","event_type":"other"}',
        b'{"event_type":"lead.created","ignored":NaN}',
    ):
        with pytest.raises(PayloadRejected):
            _payload_fields(body, webhook_id, "Fixture")
