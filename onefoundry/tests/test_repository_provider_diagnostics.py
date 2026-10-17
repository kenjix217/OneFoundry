"""Broker diagnostics retain bounded codes, never provider or command contents."""

import json

from runner.repository_provider_diagnostics import summarize


def test_summary_retains_failure_and_usage_without_private_content() -> None:
    raw = b"READY\n" + b"\n".join(
        json.dumps(value).encode()
        for value in [
            {"type": "repository_broker.denied", "reason": "repository_broker_route_denied"},
            {
                "type": "repository_broker.forwarded",
                "request_number": 1,
                "request_bytes": 1200,
                "response_bytes": 4000,
            },
            {"type": "repository_broker.denied", "reason": "repository_provider_request_limit"},
            {"type": "repository_broker.denied", "reason": "repository_private_secret"},
            {
                "type": "repository_broker.forwarded",
                "request_number": 2,
                "request_bytes": 1,
                "response_bytes": 2,
                "body": "private content",
            },
        ]
    )
    value = summarize(raw)
    assert value == {
        "type": "repository.broker",
        "forwarded_requests": 1,
        "request_bytes": 1200,
        "response_bytes": 4000,
        "last_denial": "repository_provider_request_limit",
    }
    assert "private" not in json.dumps(value)


def test_empty_malformed_and_unbounded_records_are_ignored() -> None:
    raw = (
        b"[]\ninvalid\n"
        + json.dumps(
            {
                "type": "repository_broker.forwarded",
                "request_number": True,
                "request_bytes": 10**50,
                "response_bytes": -1,
            }
        ).encode()
    )
    assert summarize(raw) == summarize(b"")


def test_only_numeric_http_status_is_retained() -> None:
    assert (
        summarize(b'{"type":"repository_broker.denied","reason":"repository_provider_http_429"}')[
            "last_denial"
        ]
        == "repository_provider_http_429"
    )
    assert (
        summarize(b'{"type":"repository_broker.denied","reason":"repository_provider_http_secret"}')[
            "last_denial"
        ]
        is None
    )
