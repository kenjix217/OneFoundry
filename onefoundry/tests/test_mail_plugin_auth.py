"""OAuth signature/audience/identity/expiry checks using generated synthetic keys."""

from pathlib import Path
from typing import Any

import anyio
import pytest

from onefoundry.mail_imap import MailError
from onefoundry.mail_plugin_auth import MailTokenVerifier, load_public_key
from tests.mail_plugin_fixtures import keypair, policy, token


def test_signed_token_is_bound_to_operator_client_resource_and_scope() -> None:
    configured = policy()
    private, public = keypair()
    verifier = MailTokenVerifier(configured, public)
    access = anyio.run(verifier.verify_token, token(private, configured))
    assert access is not None
    assert access.subject == "synthetic-owner" and access.client_id == "synthetic-dotty"
    assert access.resource == configured.public_url and access.scopes == ["mail.read"]
    assert access.claims == {"iss": configured.oauth_issuer}


def test_tunnel_token_requires_external_audience_and_maps_only_to_pinned_resource() -> None:
    configured = policy(
        public_url="http://127.0.0.1:9876/mcp",
        oauth_audience="https://tunnel.example.test/v1/mcp/synthetic",
    )
    private, public = keypair()
    verifier = MailTokenVerifier(configured, public)
    access = anyio.run(verifier.verify_token, token(private, configured, aud=configured.oauth_audience))
    assert access is not None and access.resource == configured.public_url
    assert anyio.run(verifier.verify_token, token(private, configured, aud=configured.public_url)) is None
    assert (
        anyio.run(verifier.verify_token, token(private, configured, aud="https://other.example.test")) is None
    )


@pytest.mark.parametrize(
    ("audience", "accepted"),
    [
        (["https://mail-plugin.example.test/mcp", "https://auth.example.test/userinfo"], True),
        (["https://auth.example.test/userinfo"], False),
        (["https://mail-plugin.example.test/mcp", "https://other.example.test"], False),
        (["https://mail-plugin.example.test/mcp", "https://mail-plugin.example.test/mcp"], False),
        (["https://mail-plugin.example.test/mcp", True], False),
        ([], False),
    ],
)
def test_oidc_additional_audience_requires_mcp_audience_and_exact_explicit_allowlist(
    audience: list[Any], accepted: bool
) -> None:
    configured = policy(oauth_auxiliary_audiences=("https://auth.example.test/userinfo",))
    private, public = keypair()
    verifier = MailTokenVerifier(configured, public)
    access = anyio.run(verifier.verify_token, token(private, configured, aud=audience))
    assert (access is not None) is accepted


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://other.example.test"},
        {"aud": "https://other.example.test/mcp"},
        {"aud": ["https://mail-plugin.example.test/mcp", "other"]},
        {"sub": "another-owner"},
        {"client_id": "another-client"},
        {"scope": "mail.write"},
        {"scope": ["mail.read"]},
        {"iat": 1, "exp": 2},
        {"exp": True},
        {"iat": 1},
        {"exp": 9999999999},
        {"nbf": 9999999999},
        {"sub": ["synthetic-owner"]},
        {"client_id": None},
    ],
)
def test_forged_expired_malformed_or_overbroad_tokens_fail(claims: dict[str, Any]) -> None:
    configured = policy()
    private, public = keypair()
    verifier = MailTokenVerifier(configured, public)
    assert anyio.run(verifier.verify_token, token(private, configured, **claims)) is None


def test_wrong_signature_and_unbounded_tokens_fail() -> None:
    configured = policy()
    private, _ = keypair()
    _, different = keypair()
    verifier = MailTokenVerifier(configured, different)
    assert anyio.run(verifier.verify_token, token(private, configured)) is None
    assert anyio.run(verifier.verify_token, "x" * (configured.max_token_bytes + 1)) is None
    assert anyio.run(verifier.verify_token, "not-a-token") is None


def test_verification_key_must_be_public_owned_and_nonwritable(tmp_path: Path) -> None:
    private, public = keypair()
    path = tmp_path / "public.pem"
    path.write_bytes(public)
    path.chmod(0o600)
    assert load_public_key(path) == public
    path.chmod(0o666)
    with pytest.raises(MailError, match="oauth_key_invalid"):
        load_public_key(path)
    path.chmod(0o600)
    path.write_bytes(private)
    with pytest.raises(MailError, match="oauth_key_invalid"):
        load_public_key(path)
    link = tmp_path / "link.pem"
    link.symlink_to(path)
    with pytest.raises(MailError, match="oauth_key_unavailable"):
        load_public_key(link)
    path.unlink()
    with pytest.raises(MailError, match="oauth_key_unavailable"):
        load_public_key(path)
