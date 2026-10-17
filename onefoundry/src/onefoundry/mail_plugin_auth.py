"""Verify a configured OAuth issuer's signed tokens; never issue or persist tokens."""

import os
import stat
from pathlib import Path

import jwt
from mcp.server.auth.provider import AccessToken

from onefoundry.mail_imap import MailError
from onefoundry.mail_plugin_settings import MailPluginSettings


def load_public_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
                raise MailError("oauth_key_invalid")
            data = os.read(fd, 8193)
        finally:
            os.close(fd)
        if len(data) > 8192:
            raise MailError("oauth_key_invalid")
        if not data.startswith((b"-----BEGIN PUBLIC KEY-----", b"-----BEGIN RSA PUBLIC KEY-----")):
            raise MailError("oauth_key_invalid")
        jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).prepare_key(data)
        return data
    except (OSError, ValueError, jwt.PyJWTError):
        raise MailError("oauth_key_unavailable") from None


class MailTokenVerifier:
    def __init__(self, policy: MailPluginSettings, public_key: bytes) -> None:
        self.policy, self.public_key = policy, public_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > self.policy.max_token_bytes:
            return None
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                audience=self.policy.oauth_audience or self.policy.public_url,
                issuer=self.policy.oauth_issuer,
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud", "client_id", "scope"],
                    "strict_aud": not self.policy.oauth_auxiliary_audiences,
                },
            )
            audience = claims["aud"]
            if isinstance(audience, list) and (
                any(not isinstance(item, str) for item in audience)
                or len(audience) != len(set(audience))
                or not set(audience).issubset(
                    {
                        self.policy.oauth_audience or self.policy.public_url,
                        *self.policy.oauth_auxiliary_audiences,
                    }
                )
            ):
                return None
            if (
                claims["sub"] not in self.policy.oauth_subjects
                or claims["client_id"] not in self.policy.oauth_clients
                or not isinstance(claims["scope"], str)
                or type(claims["exp"]) is not int
                or type(claims["iat"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= self.policy.max_token_seconds
            ):
                return None
            scopes = claims["scope"].split()
            if self.policy.oauth_scope not in scopes:
                return None
            return AccessToken(
                token=token,
                client_id=claims["client_id"],
                scopes=scopes,
                expires_at=claims["exp"],
                # A configured tunnel audience names this one pinned local resource.
                # JWT verification above remains exact; callers cannot choose the alias.
                resource=self.policy.public_url,
                subject=claims["sub"],
                claims={"iss": self.policy.oauth_issuer},
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None
