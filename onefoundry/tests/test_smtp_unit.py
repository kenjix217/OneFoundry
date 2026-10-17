"""Outgoing mail boundaries: one address, bounded content, TLS, and safe failure."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from onefoundry.role_tool_admission import ToolInputError
from onefoundry.settings import Settings
from onefoundry.smtp_outbound import parse_arguments
from onefoundry.smtp_schemas import OutboundProposal, SmtpAccountCreate
from onefoundry.smtp_transport import SmtpConfig, probe, submit


def test_smtp_settings_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(db_password="test").smtp_timeout_seconds == 15
    assert Settings(db_password="test", smtp_timeout_seconds=2).smtp_timeout_seconds == 2
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "7")
    assert Settings(db_password="test").smtp_timeout_seconds == 7
    assert Settings(db_password="test", smtp_timeout_seconds=3).smtp_timeout_seconds == 3
    with pytest.raises(ValidationError):
        Settings(db_password="test", smtp_timeout_seconds=0)
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test")


def test_account_and_proposal_reject_headers_and_secrets() -> None:
    valid = SmtpAccountCreate(
        label="Sales",
        host="smtp.example.test",
        username="owner@example.test",
        from_address="owner@example.test",
        password=SecretStr("app password"),
    )
    assert valid.host == "smtp.example.test"
    assert "app password" not in repr(valid)
    with pytest.raises(ValidationError):
        SmtpAccountCreate(
            label="Sales",
            host="smtp://localhost",
            username="owner@example.test",
            from_address="owner@example.test",
            password=SecretStr("password"),
        )
    fields = {
        "account_id": uuid4(),
        "run_id": uuid4(),
        "to_address": "buyer@example.test",
        "subject": "Hello",
        "body": "A plain proposal",
    }
    assert OutboundProposal.model_validate(fields).to_address == "buyer@example.test"
    for change in (
        {"to_address": "buyer@example.test,spy@example.test"},
        {"subject": "Hello\nBcc: spy@example.test"},
        {"body": "password=top-secret"},
        {"body": "x" * 12001},
    ):
        with pytest.raises(ValidationError):
            OutboundProposal.model_validate(fields | change)
    with pytest.raises(ToolInputError):
        parse_arguments(
            {"account_id": str(uuid4()), "to_address": "x@example.test", "subject": "Hi", "body": "token=abc"}
        )


def test_ssl_submission_uses_one_recipient_and_verified_context() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    client.send_message.return_value = {}
    config = SmtpConfig("smtp.example.test", 465, "ssl", "owner", "private", 5)
    with patch("onefoundry.smtp_transport.smtplib.SMTP_SSL", return_value=client) as factory:
        submit(
            config, "owner@example.test", "buyer@example.test", "Hello", "Body", "<fixed@onefoundry.local>"
        )
    assert factory.call_args.kwargs["context"].verify_mode != 0
    client.login.assert_called_once_with("owner", "private")
    assert client.send_message.call_args.kwargs["to_addrs"] == ["buyer@example.test"]
    assert client.send_message.call_args.args[0]["Message-ID"] == "<fixed@onefoundry.local>"


def test_starttls_probe_requires_tls_before_login() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    config = SmtpConfig("smtp.example.test", 587, "starttls", "owner", "private", 5)
    with patch("onefoundry.smtp_transport.smtplib.SMTP", return_value=client):
        probe(config)
    assert client.method_calls.index(
        next(call for call in client.method_calls if call[0] == "starttls")
    ) < client.method_calls.index(next(call for call in client.method_calls if call[0] == "login"))
