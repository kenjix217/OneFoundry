"""Boundary validation and SDK exclusion for proposal-only role comments."""

from uuid import uuid4

import pytest

from onefoundry.role_github_pr_comment import parse_arguments
from onefoundry.role_tool_admission import LIVE_SDK_EXCLUDED_IDS, ToolInputError


def _arguments(comment: str = "Review this.") -> dict[str, object]:
    return {
        "source_id": str(uuid4()),
        "number": 1,
        "comment": comment,
        "idempotency_key": str(uuid4()),
    }


def test_comment_arguments_are_exact_and_sdk_does_not_offer_approval_wait() -> None:
    arguments = _arguments()
    assert parse_arguments(arguments) == arguments
    assert "github_pr_comment_v1" in LIVE_SDK_EXCLUDED_IDS
    for invalid in (
        {**arguments, "number": True},
        {**arguments, "number": "1"},
        {**arguments, "extra": "value"},
        {**arguments, "comment": "line\rbreak"},
        {**arguments, "comment": "x" * 16_001},
    ):
        with pytest.raises(ToolInputError):
            parse_arguments(invalid)
