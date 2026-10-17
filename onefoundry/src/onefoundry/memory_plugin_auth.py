"""Verify signed, short-lived resource tokens against server-owned project grants."""

import os
import stat
from pathlib import Path

import jwt
from mcp.server.auth.provider import AccessToken

from onefoundry.memory_plugin_settings import MemoryGrant, MemoryPluginSettings


def load_public_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
                raise ValueError("verification key must be an owned regular file without other writers")
            data = os.read(fd, 8193)
        finally:
            os.close(fd)
        if len(data) > 8192 or not data.startswith(b"-----BEGIN PUBLIC KEY-----"):
            raise ValueError("use a bounded RSA public verification key")
        jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).prepare_key(data)
        return data
    except (OSError, jwt.PyJWTError):
        raise ValueError("memory OAuth verification key unavailable") from None


def granted(policy: MemoryPluginSettings, subject: str | None, client_id: str) -> MemoryGrant | None:
    return next(
        (grant for grant in policy.grants if grant.subject == subject and grant.client_id == client_id), None
    )


class MemoryTokenVerifier:
    def __init__(self, policy: MemoryPluginSettings, public_key: bytes) -> None:
        self.policy, self.public_key = policy, public_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > self.policy.max_token_bytes:
            return None
        try:
            audience = self.policy.oauth_audience or self.policy.public_url
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                audience=audience,
                issuer=self.policy.oauth_issuer,
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud", "client_id", "scope"],
                    "strict_aud": not self.policy.oauth_auxiliary_audiences,
                },
            )
            token_audiences = claims["aud"]
            if isinstance(token_audiences, list) and (
                any(not isinstance(item, str) for item in token_audiences)
                or len(set(token_audiences)) != len(token_audiences)
                or not set(token_audiences).issubset({audience, *self.policy.oauth_auxiliary_audiences})
            ):
                return None
            if (
                type(claims["exp"]) is not int
                or type(claims["iat"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= self.policy.max_token_seconds
                or not isinstance(claims["scope"], str)
                or granted(self.policy, claims["sub"], claims["client_id"]) is None
            ):
                return None
            scopes = claims["scope"].split()
            if "memory.read" not in scopes:
                return None
            return AccessToken(
                token=token,
                client_id=claims["client_id"],
                subject=claims["sub"],
                scopes=scopes,
                expires_at=claims["exp"],
                resource=self.policy.public_url,
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None
