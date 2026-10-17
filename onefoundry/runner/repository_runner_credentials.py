"""Read approved account material without changing local authentication state."""

from __future__ import annotations

import json
import os
import stat

from runner.repository_runner_settings import RepositoryRunnerSettings


def _header(value: object) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("repository_provider_credential_invalid")
    return value


def broker_credential(settings: RepositoryRunnerSettings) -> tuple[bytes, str | None]:
    settings.validate_host()
    descriptor = os.open(settings.credential_file, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= 8193
        ):
            raise ValueError("repository_provider_credential_invalid")
        raw = os.read(descriptor, 8194)
    finally:
        os.close(descriptor)
    if len(raw) > 8193:
        raise ValueError("repository_provider_credential_invalid")
    if settings.auth_kind == "api_key":
        return _header(raw.decode("ascii").strip()).encode("ascii"), None
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get("auth_mode") != "chatgpt":
            raise ValueError("repository_provider_credential_invalid")
        tokens = state.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError("repository_provider_credential_invalid")
        return _header(tokens.get("access_token")).encode("ascii"), _header(tokens.get("account_id"))
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("repository_provider_credential_invalid") from None
