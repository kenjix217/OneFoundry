"""Conservative rejection of credential-shaped text before role/model exposure."""

from __future__ import annotations

import re

from onefoundry.workflow_schemas import SECRET_PATTERN

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:^|\b)(?:[a-z][a-z0-9]*[_-])*(?:database[_-]?(?:url|uri|dsn)|db[_-]?(?:url|uri|dsn|password)|"
    r"auth[_-]?token|_authToken|client[_-]?secret|secret[_-]?key|private[_-]?key|"
    r"access[_-]?key(?:[_-]?id)?)\s*[=:]\s*['\"]?\S+",
    re.MULTILINE,
)
_CREDENTIAL_URI = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqps?|https?)"
    r"://[^\s/@:]+:[^\s/@]+@"
)


def contains_sensitive_text(value: str) -> bool:
    return bool(
        SECRET_PATTERN.search(value) or _CREDENTIAL_ASSIGNMENT.search(value) or _CREDENTIAL_URI.search(value)
    )
