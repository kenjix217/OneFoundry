"""Credential-shaped repository and PR text is rejected before role output."""

import pytest

from onefoundry.role_sensitive_text import contains_sensitive_text


@pytest.mark.parametrize(
    "value",
    [
        "DB_URI=postgresql://alice:abc123@db.internal/company",
        'DATABASE_URL: "postgresql://alice:abc123@db.internal/company"',
        "//registry.npmjs.org/:_authToken=npm_somethingsecret123456",
        "https://user:secretvalue@example.com/private",
        "client_secret = keep-this-private",
        "AWS_SECRET_ACCESS_KEY=" + (("A1" * 20)[:40]) + "",
    ],
)
def test_rejects_common_credential_shapes(value: str) -> None:
    assert contains_sensitive_text(value)


def test_accepts_plain_code_without_credential_shape() -> None:
    assert not contains_sensitive_text("def calculate(value: int) -> int:\n    return value + 1\n")
