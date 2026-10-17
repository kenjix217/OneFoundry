"""Stable installed tool identity separate from a package's local tool name."""

import base64
import hashlib
import json


def canonical_tool_id(package_key: str, declared_tool_id: str) -> str:
    """Retain the full tuple digest within the provider's 64-character limit."""
    identity = json.dumps((package_key, declared_tool_id), ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    encoded_digest = base64.b32encode(hashlib.sha256(identity).digest()).decode("ascii").rstrip("=").lower()
    return f"ext_{encoded_digest}_{declared_tool_id[:7]}"
