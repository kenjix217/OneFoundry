"""Receipt rejection and opt-in real-Docker offline synthetic browser proof."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess

import pytest

from runner import browser_fixture_proof as proof
from runner.browser_fixture_child import MARKER, MAX_PNG
from runner.browser_fixture_proof import (
    PNG_SIGNATURE,
    URL,
    BrowserFixtureError,
    run_browser_fixture_proof,
    validate_child_receipt,
)


def _sample() -> dict[str, object]:
    png = PNG_SIGNATURE + b"fake body" + b"IEND\xaeB`\x82"
    return {
        "marker": MARKER,
        "title": MARKER,
        "text": MARKER,
        "final_url": URL,
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "png_base64": base64.b64encode(png).decode(),
        "no_default_route": True,
        "no_host_paths": True,
        "hostile_request_blocked": True,
        "popup_count": 0,
        "download_count": 0,
    }


def test_receipt_accepts_bounded_synthetic_artifact() -> None:
    value = validate_child_receipt(json.dumps(_sample()), "sha256:test")
    assert value.final_url == URL
    assert hashlib.sha256(value.png).hexdigest() == value.png_sha256


@pytest.mark.parametrize(
    "field,value",
    [
        ("final_url", "http://example.invalid/"),
        ("hostile_request_blocked", False),
        ("no_default_route", False),
        ("no_host_paths", False),
        ("popup_count", 1),
        ("download_count", 1),
        ("png_sha256", "0" * 64),
        ("png_base64", "bad!"),
        ("png_base64", base64.b64encode(b"X" * (MAX_PNG + 1)).decode()),
    ],
)
def test_receipt_rejects_hostile_or_invalid_result(field: str, value: object) -> None:
    sample = _sample()
    sample[field] = value
    with pytest.raises(BrowserFixtureError, match="browser_receipt_invalid|browser_log_too_large"):
        validate_child_receipt(json.dumps(sample), "sha256:test")


def test_receipt_rejects_unknown_fields() -> None:
    sample = _sample()
    sample["secret"] = "bad"
    with pytest.raises(BrowserFixtureError, match="browser_receipt_invalid"):
        validate_child_receipt(json.dumps(sample), "sha256:test")


def test_fixture_proof_requires_an_exact_image_id() -> None:
    with pytest.raises(BrowserFixtureError, match="browser_config_invalid"):
        run_browser_fixture_proof("onefoundry-browser-fixture:offline-v1")


@pytest.mark.parametrize("kind", ["container", "network"])
@pytest.mark.parametrize("remove_fails", [False, True])
def test_cleanup_accepts_only_confirmed_not_found(
    monkeypatch: pytest.MonkeyPatch, kind: str, remove_fails: bool
) -> None:
    name = "onefoundry-browser-test-resource"
    stderr = (
        f"Error response from daemon: No such container: {name}"
        if kind == "container"
        else f"Error response from daemon: network {name} not found"
    )

    def remove(*_args: object, **_kwargs: object) -> str:
        if remove_fails:
            raise BrowserFixtureError("docker_rm_failed")
        return name

    monkeypatch.setattr(proof, "_docker", remove)
    monkeypatch.setattr(
        proof,
        "_docker_result",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "[]\n", stderr),
    )
    proof._remove("docker", kind, name)


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "[]\n", "Cannot connect to the Docker daemon"),
        subprocess.CompletedProcess([], 0, "not-json", ""),
        subprocess.CompletedProcess([], 0, "[]", ""),
    ],
)
def test_cleanup_rejects_unverified_inspect(
    monkeypatch: pytest.MonkeyPatch, result: subprocess.CompletedProcess[str]
) -> None:
    monkeypatch.setattr(proof, "_docker", lambda *_args, **_kwargs: "removed")
    monkeypatch.setattr(proof, "_docker_result", lambda *_args, **_kwargs: result)
    with pytest.raises(BrowserFixtureError, match="browser_cleanup_unverified"):
        proof._remove("docker", "container", "onefoundry-browser-test-resource")


def test_cleanup_rejects_inspect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof, "_docker", lambda *_args, **_kwargs: "removed")

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise BrowserFixtureError("docker_unavailable_or_timed_out")

    monkeypatch.setattr(proof, "_docker_result", timeout)
    with pytest.raises(BrowserFixtureError, match="browser_cleanup_unverified"):
        proof._remove("docker", "container", "onefoundry-browser-test-resource")


def test_cleanup_rejects_surviving_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof, "_docker", lambda *_args, **_kwargs: "removed")
    monkeypatch.setattr(
        proof,
        "_docker_result",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, '[{"Id":"still-here"}]', ""),
    )
    with pytest.raises(BrowserFixtureError, match="browser_cleanup_failed"):
        proof._remove("docker", "container", "onefoundry-browser-test-resource")


@pytest.mark.integration
def test_offline_browser_fixture_real_docker() -> None:
    image = os.environ.get("ONEFOUNDRY_BROWSER_TEST_IMAGE")
    if not image:
        pytest.skip("Set ONEFOUNDRY_BROWSER_TEST_IMAGE to the locally built synthetic image")
    result = run_browser_fixture_proof(image)
    assert result.marker == MARKER
    assert result.title == MARKER
    assert result.text == MARKER
    assert result.final_url == URL
    assert result.png.startswith(PNG_SIGNATURE)
    assert result.no_default_route and result.no_host_paths and result.hostile_request_blocked
