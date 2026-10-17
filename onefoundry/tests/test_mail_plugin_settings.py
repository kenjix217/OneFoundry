"""Mail plugin startup requires explicit scope, hosting/auth bindings, and bounded values."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from onefoundry.mail_plugin_settings import MailPluginSettings
from tests.mail_plugin_fixtures import policy


def test_security_configuration_has_no_permissive_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MailPluginSettings.model_fields:
        monkeypatch.delenv(f"MAIL_PLUGIN_{name.upper()}", raising=False)
    with pytest.raises(ValidationError):
        MailPluginSettings(**{})
    values = policy().model_dump()
    for name, field in MailPluginSettings.model_fields.items():
        if field.is_required():
            missing = {key: value for key, value in values.items() if key != name}
            with pytest.raises(ValidationError):
                MailPluginSettings(**missing)


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("listen_host", "0.0.0.0"),  # noqa: S104 - forbidden bind address
        ("public_url", "http://mail.example.test/mcp"),
        ("public_url", "https://secret@mail.example.test/mcp"),
        ("public_url", "https://x.test/other"),
        ("oauth_issuer", "https://x.test/?token=sensitive"),
        ("folders", ()),
        ("folders", ("INBOX", "INBOX")),
        ("folders", ("INBOX\r\nSELECT",)),
        ("folders", ("ü",)),
        ("oauth_subjects", ()),
        ("oauth_clients", ()),
        ("oauth_scope", "x\r"),
        ("actor_id", ""),
        ("username", "x\r\n"),
        ("imap_host", "https://imap.example.test"),
        ("imap_host", "IMAP.EXAMPLE.TEST"),
        ("imap_port", 0),
        ("listen_port", 65536),
        ("oauth_public_key_path", Path("relative.pem")),
        ("project_id", "invalid"),
        ("account_id", "invalid"),
        ("id_key_id", "invalid"),
        ("max_results", 21),
        ("uid_window", 0),
        ("header_bytes", 0),
        ("body_chars", 10001),
        ("deadline_seconds", 0),
        ("max_calls_per_minute", 0),
        ("max_request_bytes", 65537),
        ("max_token_bytes", 0),
        ("max_session_bytes", 4194305),
        ("max_token_seconds", 0),
    ],
)
def test_invalid_setting_fails(name: str, bad: Any) -> None:
    with pytest.raises(ValidationError):
        policy(**{name: bad})


@pytest.mark.parametrize("name", list(MailPluginSettings.model_fields))
def test_explicit_settings_override_environment(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = (
        policy(**{"oauth_audience": "https://tunnel.example.test/v1/mcp/synthetic"})
        if name == "oauth_audience"
        else policy()
    )
    value = getattr(configured, name)
    encoded = json.dumps(list(value)) if isinstance(value, tuple) else str(value)
    monkeypatch.setenv(f"MAIL_PLUGIN_{name.upper()}", encoded)
    args = configured.model_dump()
    args.pop(name)
    assert getattr(MailPluginSettings(**args), name) == value
    monkeypatch.setenv(f"MAIL_PLUGIN_{name.upper()}", "[]" if isinstance(value, tuple) else "invalid")
    assert getattr(MailPluginSettings(**configured.model_dump()), name) == value


def test_tunnel_metadata_is_loopback_bound_and_audience_is_explicit() -> None:
    configured = policy(
        public_url="http://127.0.0.1:9876/mcp",
        oauth_audience="https://tunnel.example.test/v1/mcp/synthetic",
    )
    assert configured.public_url == "http://127.0.0.1:9876/mcp"
    assert configured.oauth_audience == "https://tunnel.example.test/v1/mcp/synthetic"
    assert policy().oauth_audience is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"public_url": "http://127.0.0.1:9876/mcp", "oauth_audience": None},
        {"public_url": "http://127.0.0.1:9877/mcp"},
        {"public_url": "http://192.0.2.1:9876/mcp"},
        {"public_url": "http://localhost:9876/mcp"},
        {"public_url": "http://127.0.0.1:9876/mcp?secret=x"},
        {"oauth_audience": "http://tunnel.example.test/v1/mcp/synthetic"},
        {"oauth_audience": "https://secret@tunnel.example.test/v1/mcp/synthetic"},
        {"oauth_audience": "https://tunnel.example.test/v1/mcp/synthetic?secret=x"},
        {"oauth_auxiliary_audiences": ("http://auth.example.test/userinfo",)},
        {"oauth_auxiliary_audiences": ("https://secret@auth.example.test/userinfo",)},
        {"oauth_auxiliary_audiences": ("https://auth.example.test/userinfo?secret=x",)},
        {"oauth_auxiliary_audiences": ("https://auth.example.test/one", "https://auth.example.test/two")},
        {"oauth_auxiliary_audiences": ("https://tunnel.example.test/v1/mcp/synthetic",)},
    ],
)
def test_tunnel_configuration_rejects_unbound_or_invalid_urls(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        policy(
            **{
                "public_url": "http://127.0.0.1:9876/mcp",
                "oauth_audience": "https://tunnel.example.test/v1/mcp/synthetic",
                **overrides,
            }
        )
