"""Shared synthetic configuration and keys; no real account or credentials."""

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onefoundry.mail_imap import MailConfig
from onefoundry.mail_plugin_contracts import MessageIds
from onefoundry.mail_plugin_gateway import MailPluginGateway
from onefoundry.mail_plugin_settings import MailPluginSettings
from onefoundry.mail_store import MailStore
from onefoundry.tool_gateway import ToolGateway


def policy(**overrides: Any) -> MailPluginSettings:
    values: dict[str, Any] = {
        "project_id": uuid4(),
        "account_id": uuid4(),
        "username": "owner@example.test",
        "imap_host": "127.0.0.1",
        "imap_port": 993,
        "folders": ("INBOX", "Sent"),
        "actor_id": "synthetic-mail-reader",
        "id_key_id": uuid4(),
        "public_url": "https://mail-plugin.example.test/mcp",
        "oauth_issuer": "https://auth.example.test",
        "oauth_public_key_path": Path("/synthetic/test-public.pem"),
        "oauth_subjects": ("synthetic-owner",),
        "oauth_clients": ("synthetic-dotty",),
        "listen_port": 9876,
    }
    values.update(overrides)
    return MailPluginSettings(**values)


def keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


def token(private: bytes, configured: MailPluginSettings, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": configured.oauth_issuer,
        "aud": configured.public_url,
        "sub": configured.oauth_subjects[0],
        "client_id": configured.oauth_clients[0],
        "scope": configured.oauth_scope,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256")


def mock_gateway(
    configured: MailPluginSettings, config: MailConfig, **options: Any
) -> tuple[MailPluginGateway, MagicMock, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    owner = MagicMock(spec=ToolGateway)
    owner.engine = MagicMock()
    owner.actor_id = configured.actor_id
    owner._event.side_effect = lambda *args: events.append(dict(args[5]))
    store = MagicMock(spec=MailStore)
    store.read_only_config.return_value = config
    ids = MessageIds(b"synthetic-signing-key-at-least-32-bytes", configured.project_id, configured.account_id)
    return MailPluginGateway(owner, store, configured, ids, **options), store, events
