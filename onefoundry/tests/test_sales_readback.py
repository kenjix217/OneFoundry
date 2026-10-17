"""Mailpit readback uses the bytes Mailpit actually returns over its API."""

import hashlib
import io
import json
from unittest.mock import Mock, patch

import pytest

from onefoundry.sales_store import RECIPIENT, SalesStore


def response(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "body", ["Synthetic line one\n\nSynthetic line two", "Synthetic line one\nSynthetic line two\n"]
)
def test_mailpit_crlf_body_matches_saved_lf_draft(body: str) -> None:
    draft = {
        "subject": "Local synthetic preview",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    listing = {
        "messages": [
            {
                "ID": "local-message-1",
                "Subject": draft["subject"],
                "To": [{"Address": RECIPIENT}],
            }
        ]
    }
    detail = {"Text": body.replace("\n", "\r\n") + "\r\n"}
    store = SalesStore(Mock(), "mailpit", 1025, 8025)
    with patch("onefoundry.sales_store.urlopen", side_effect=[response(listing), response(detail)]) as get:
        assert store._mailpit_readback(draft) is True
    assert get.call_count == 2


def test_mailpit_different_body_or_recipient_does_not_confirm_delivery() -> None:
    draft = {
        "subject": "Local synthetic preview",
        "body_sha256": hashlib.sha256(b"Expected body").hexdigest(),
    }
    store = SalesStore(Mock(), "mailpit", 1025, 8025)
    listing = {
        "messages": [
            {
                "ID": "local-message-1",
                "Subject": draft["subject"],
                "To": [{"Address": RECIPIENT}],
            }
        ]
    }
    with patch(
        "onefoundry.sales_store.urlopen",
        side_effect=[response(listing), response({"Text": "Wrong body\r\n"})],
    ):
        assert store._mailpit_readback(draft) is False
    listing["messages"] = [
        {
            "ID": "local-message-1",
            "Subject": draft["subject"],
            "To": [{"Address": "someone-else@onefoundry.invalid"}],
        }
    ]
    with patch("onefoundry.sales_store.urlopen", return_value=response(listing)) as get:
        assert store._mailpit_readback(draft) is False
    assert get.call_count == 1


@pytest.mark.parametrize(
    "listing",
    [
        {"messages": None},
        {"messages": [None]},
        {"messages": [{"Subject": "Local synthetic preview", "To": None}]},
        {"messages": [{"Subject": "Local synthetic preview", "To": [None]}]},
    ],
)
def test_mailpit_malformed_listing_is_readback_failure(listing: object) -> None:
    draft = {"subject": "Local synthetic preview", "body_sha256": hashlib.sha256(b"body").hexdigest()}
    store = SalesStore(Mock(), "mailpit", 1025, 8025)
    with patch("onefoundry.sales_store.urlopen", return_value=response(listing)) as get:
        with pytest.raises(ValueError, match="Invalid Mailpit"):
            store._mailpit_readback(draft)
    assert get.call_count == 1


@pytest.mark.parametrize("detail", [{"Text": None}, {"Text": ["body"]}, ["body"]])
def test_mailpit_malformed_detail_is_readback_failure(detail: object) -> None:
    draft = {"subject": "Local synthetic preview", "body_sha256": hashlib.sha256(b"body").hexdigest()}
    listing = {
        "messages": [{"ID": "local-message-1", "Subject": draft["subject"], "To": [{"Address": RECIPIENT}]}]
    }
    store = SalesStore(Mock(), "mailpit", 1025, 8025)
    with patch("onefoundry.sales_store.urlopen", side_effect=[response(listing), response(detail)]) as get:
        with pytest.raises(ValueError, match="Invalid Mailpit"):
            store._mailpit_readback(draft)
    assert get.call_count == 2
