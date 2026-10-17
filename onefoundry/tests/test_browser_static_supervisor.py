"""Static browser fixture staging and result admission without a live Docker call."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from onefoundry.browser_target import BrowserTarget, BrowserTargetLimits
from runner.browser_static_child import inspect
from runner.browser_static_supervisor import (
    StaticBrowserError,
    _check_container,
    _input,
    run_static_browser_inspection,
    validate_receipt,
)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
LIMITS = BrowserTargetLimits(2048, 1920, 1200, 262_144, 30, 300)
HTML = b"<!doctype html><title>Fixture</title><main>Approved page</main>"
CSS = b"main { color: navy; }"


def target() -> BrowserTarget:
    return BrowserTarget.bind(
        project_id=uuid4(),
        run_id=uuid4(),
        actor_id="operator",
        approved_url="http://127.0.0.1:8765/approved.html",
        expires_at=NOW + timedelta(seconds=120),
        idempotency_key=uuid4(),
        viewport_width=800,
        viewport_height=600,
        max_page_bytes=262_144,
        timeout_seconds=10,
        limits=LIMITS,
        now=NOW,
    )


def receipt(source_sha256: str) -> dict[str, object]:
    return {
        "title": "Fixture",
        "text": "Approved page",
        "final_url": "http://127.0.0.1:8765/approved.html",
        "status": 200,
        "blocked_request_count": 2,
        "popup_count": 0,
        "download_count": 0,
        "no_default_route": True,
        "no_host_paths": True,
        "source_sha256": source_sha256,
    }


def test_exact_fixture_payload_and_text_only_receipt() -> None:
    approved = target()
    payload, digest = _input(approved, HTML, CSS, NOW)
    assert json.loads(payload) == {
        "url": approved.approved_url,
        "html": HTML.decode(),
        "css": CSS.decode(),
        "width": 800,
        "height": 600,
        "timeout_ms": 10_000,
    }
    assert digest == hashlib.sha256(HTML + b"\x00" + CSS).hexdigest()
    result = validate_receipt(json.dumps(receipt(digest)).encode(), approved, digest, "sha256:image", now=NOW)
    assert result.binding_digest == approved.binding_digest
    assert result.text == "Approved page"
    assert not hasattr(result, "png")


@pytest.mark.parametrize(
    "change",
    [
        {"approved_url": "http://localhost:8765/approved.html"},
        {"max_page_bytes": 1},
        {"expires_at": NOW + timedelta(seconds=1)},
    ],
)
def test_rejects_unsupported_expired_or_oversized_target(change: dict[str, object]) -> None:
    approved = replace(target(), **cast(Any, change))
    with pytest.raises(StaticBrowserError):
        _input(approved, HTML, CSS, NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    "html,css",
    [
        (b"", CSS),
        (b"\xff", CSS),
        (HTML, b"</style><script>alert(1)</script>"),
        (b"<div>password=topsecret</div>", CSS),
    ],
)
def test_rejects_unsafe_source(html: bytes, css: bytes) -> None:
    with pytest.raises(StaticBrowserError):
        _input(target(), html, css, NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("final_url", "http://127.0.0.1:8765/other"),
        ("status", 302),
        ("status", True),
        ("popup_count", 1),
        ("download_count", 1),
        ("no_default_route", False),
        ("no_host_paths", False),
        ("source_sha256", "0" * 64),
        ("text", "password=topsecret"),
        ("blocked_request_count", -1),
    ],
)
def test_rejects_untrusted_child_result(field: str, value: object) -> None:
    approved = target()
    _, digest = _input(approved, HTML, CSS, NOW)
    raw = receipt(digest)
    raw[field] = value
    with pytest.raises(StaticBrowserError, match="browser_receipt_invalid"):
        validate_receipt(json.dumps(raw).encode(), approved, digest, "sha256:image", now=NOW)


def test_rejects_extra_result_fields_and_expiry() -> None:
    approved = target()
    _, digest = _input(approved, HTML, CSS, NOW)
    raw = receipt(digest)
    raw["png_base64"] = "secret artifact"
    with pytest.raises(StaticBrowserError, match="browser_receipt_invalid"):
        validate_receipt(json.dumps(raw).encode(), approved, digest, "sha256:image", now=NOW)
    with pytest.raises(StaticBrowserError, match="browser_receipt_invalid"):
        validate_receipt(
            json.dumps(receipt(digest)).encode(),
            approved,
            digest,
            "sha256:image",
            now=approved.expires_at,
        )


def test_rejects_container_with_mount_or_network() -> None:
    host = {
        "Binds": None,
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "PidsLimit": 128,
        "Memory": 768 * 1024 * 1024,
        "NanoCpus": 1_000_000_000,
        "PortBindings": None,
        "Tmpfs": {"/tmp": "rw,nosuid,size=256m"},  # noqa: S108
    }
    value = {"Image": "sha256:image", "Mounts": [], "HostConfig": host, "Config": {"User": "1000:1000"}}
    _check_container(value, "sha256:image")
    with pytest.raises(StaticBrowserError, match="boundary"):
        _check_container({**value, "Mounts": [{"Source": "/secret"}]}, "sha256:image")
    with pytest.raises(StaticBrowserError, match="boundary"):
        _check_container({**value, "HostConfig": {**host, "NetworkMode": "bridge"}}, "sha256:image")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid:8765/approved.html",
        "file:///tmp/approved.html",
        "http://127.0.0.1:8765/approved.html?x=1",
    ],
)
def test_child_rejects_unapproved_network_or_file_target_before_browser_import(url: str) -> None:
    with pytest.raises(ValueError, match="input_bounds_invalid"):
        inspect(
            {
                "url": url,
                "html": HTML.decode(),
                "css": CSS.decode(),
                "width": 800,
                "height": 600,
                "timeout_ms": 10_000,
            }
        )


@pytest.mark.integration
def test_static_browser_real_docker_opt_in() -> None:
    image = os.environ.get("ONEFOUNDRY_STATIC_BROWSER_TEST_IMAGE")
    if not image:
        pytest.skip("Set ONEFOUNDRY_STATIC_BROWSER_TEST_IMAGE to a locally built static browser image ID")
    now = datetime.now(UTC)
    approved = replace(target(), issued_at=now, expires_at=now + timedelta(seconds=120))
    result = run_static_browser_inspection(approved, html=HTML, css=CSS, image=image)
    assert result.title == "Fixture"
    assert result.text == "Approved page"
    assert result.final_url == approved.approved_url
