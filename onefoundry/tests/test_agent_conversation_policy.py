"""Agent selection keeps old replay hashes and immutable saved-conversation ownership."""

import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.chief_workspace_messages import fingerprint
from onefoundry.chief_workspace_schemas import ChiefMessage


def test_old_chief_request_fingerprint_is_unchanged() -> None:
    old = {
        "client_message_id": str(uuid4()),
        "order_id": None,
        "expected_version": None,
        "expected_sequence": 0,
        "body": "Prepare a brief.",
    }
    digest = hashlib.sha256(json.dumps(old, separators=(",", ":")).encode()).hexdigest()
    assert fingerprint(ChiefMessage.model_validate(old)) == digest
    assert fingerprint(ChiefMessage.model_validate({**old, "profile_id": None})) == digest
    assert fingerprint(ChiefMessage.model_validate({**old, "profile_id": str(uuid4())})) != digest


def test_agent_selection_is_validated_and_only_allowed_for_a_new_conversation() -> None:
    selected = {"client_message_id": uuid4(), "body": "Discuss the brief.", "profile_id": uuid4()}
    assert ChiefMessage.model_validate(selected).profile_id == selected["profile_id"]
    for invalid in (
        {"profile_id": "bad-id"},
        {"order_id": uuid4(), "expected_version": 1},
        {"body": "   "},
        {"unexpected": True},
    ):
        with pytest.raises(ValidationError):
            ChiefMessage.model_validate({**selected, **invalid})
