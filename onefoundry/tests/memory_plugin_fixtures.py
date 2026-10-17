"""Synthetic identities and ephemeral OAuth keys; no company content or credentials."""

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from onefoundry.memory_plugin_settings import MemoryGrant, MemoryPluginSettings


def policy(**changes: Any) -> MemoryPluginSettings:
    grant = MemoryGrant(
        subject="synthetic-operator",
        client_id="synthetic-codex",
        actor_id="external-memory:codex",
        project_ids=(uuid4(),),
    )
    return MemoryPluginSettings.model_validate(
        {
            "public_url": "https://memory.example.test/mcp",
            "oauth_issuer": "https://issuer.example.test/",
            "oauth_public_key_path": Path("/fixture/public.pem"),
            "listen_port": 9877,
            "grants": (grant,),
            **changes,
        }
    )


def keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )


def token(private: bytes, configured: MemoryPluginSettings, **changes: Any) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": configured.grants[0].subject,
            "client_id": configured.grants[0].client_id,
            "iss": configured.oauth_issuer,
            "aud": configured.oauth_audience or configured.public_url,
            "scope": "memory.read",
            "iat": now,
            "exp": now + 120,
            **changes,
        },
        private,
        algorithm="RS256",
    )
