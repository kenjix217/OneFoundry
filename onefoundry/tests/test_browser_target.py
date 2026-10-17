"""Exact, offline browser inspection target admission."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from onefoundry.browser_target import BrowserTarget, BrowserTargetError, BrowserTargetLimits

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
LIMITS = BrowserTargetLimits(
    max_url_chars=2048,
    max_viewport_width=1920,
    max_viewport_height=1200,
    max_page_bytes=1_000_000,
    max_timeout_seconds=30,
    max_lifetime_seconds=300,
)


def bind(**changes: object) -> BrowserTarget:
    arguments: dict[str, object] = {
        "project_id": uuid4(),
        "run_id": uuid4(),
        "actor_id": "operator",
        "approved_url": "http://127.0.0.1:8765/preview/page-1",
        "expires_at": NOW + timedelta(seconds=60),
        "idempotency_key": uuid4(),
        "viewport_width": 1280,
        "viewport_height": 800,
        "max_page_bytes": 500_000,
        "timeout_seconds": 15,
        "limits": LIMITS,
        "now": NOW,
    }
    arguments.update(changes)
    return BrowserTarget.bind(**arguments)  # type: ignore[arg-type]


def test_exact_target_binding_and_digest() -> None:
    project_id = uuid4()
    run_id = uuid4()
    key = uuid4()
    target = bind(project_id=project_id, run_id=run_id, idempotency_key=key)
    again = bind(project_id=project_id, run_id=run_id, idempotency_key=key)
    assert target.project_id == project_id
    assert target.run_id == run_id
    assert target.origin == "http://127.0.0.1:8765"
    assert target.path == "/preview/page-1"
    assert target.binding_digest == again.binding_digest
    assert len(target.binding_digest) == 64
    target.check_navigation(target.approved_url, now=NOW)
    assert bind(approved_url="http://[::1]:8765/").origin == "http://[::1]:8765"
    assert bind(approved_url="http://localhost:8765/").path == "/"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765/preview",
        "file:///tmp/index.html",
        "http://example.com:8765/preview",
        "http://169.254.169.254:80/",
        "http://127.0.0.1/",
        "http://127.0.0.1:0/",
        "http://127.0.0.1:65536/",
        "http://user:pass@127.0.0.1:8765/",
        "http://127.0.0.1:8765/?token=secret",
        "http://127.0.0.1:8765/#fragment",
        "http://127.0.0.1:8765/../secret",
        "http://127.0.0.1:8765/%2e%2e/secret",
        "http://127.0.0.1:8765//secret",
        "http://127.0.0.1:8765/back\\slash",
        "http://127.0.0.1:8765/space here",
        "http://127.0.0.1:8765/preview\nHost: evil.test",
        "http://LOCALHOST:8765/",
        "http://127.0.0.1:8765",
    ],
)
def test_rejects_noncanonical_or_unsafe_target(url: str) -> None:
    with pytest.raises(BrowserTargetError):
        bind(approved_url=url)


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "not-a-uuid"},
        {"run_id": UUID(int=0)},
        {"actor_id": ""},
        {"actor_id": "operator\nspoof"},
        {"idempotency_key": "not-a-uuid"},
        {"viewport_width": 0},
        {"viewport_width": 1921},
        {"viewport_height": 1201},
        {"max_page_bytes": 1_000_001},
        {"timeout_seconds": 31},
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(seconds=301)},
        {"expires_at": datetime(2026, 9, 27, 12)},
    ],
)
def test_rejects_invalid_identity_bounds_and_expiry(change: dict[str, object]) -> None:
    with pytest.raises(BrowserTargetError):
        bind(**change)


def test_accepts_boundaries() -> None:
    target = bind(
        viewport_width=1920,
        viewport_height=1200,
        max_page_bytes=1_000_000,
        timeout_seconds=30,
        expires_at=NOW + timedelta(seconds=300),
    )
    target.check_navigation(target.approved_url, now=NOW + timedelta(seconds=299))


def test_direct_construction_cannot_expand_approved_lifetime() -> None:
    target = bind()
    with pytest.raises(BrowserTargetError, match="lifetime"):
        replace(target, expires_at=NOW + timedelta(seconds=301))
    with pytest.raises(BrowserTargetError, match="lifetime"):
        replace(target, issued_at=target.expires_at)


def test_rejects_redirect_drift_and_expired_navigation() -> None:
    target = bind()
    with pytest.raises(BrowserTargetError, match="redirect"):
        target.check_navigation(target.approved_url, redirect=True, now=NOW)
    with pytest.raises(BrowserTargetError, match="drift"):
        target.check_navigation("http://127.0.0.1:8765/preview/other", now=NOW)
    with pytest.raises(BrowserTargetError, match="drift"):
        target.check_navigation("http://127.0.0.1:8766/preview/page-1", now=NOW)
    with pytest.raises(BrowserTargetError, match="expired"):
        target.check_navigation(target.approved_url, now=target.expires_at)
