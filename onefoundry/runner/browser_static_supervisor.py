"""Host-only, networkless inspection of one trusted static HTML/CSS fixture.

The caller must resolve an operator-approved fixture and BrowserTarget from its
owner record. Role arguments must never supply a URL or source bytes here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from onefoundry.browser_target import BrowserTarget, BrowserTargetError
from onefoundry.role_sensitive_text import contains_sensitive_text
from runner.browser_fixture_proof import BASE_DIGEST, _docker, _inspect, _remove
from runner.browser_static_child import MAX_TEXT, MAX_TITLE

_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_RECEIPT = 16_384


class StaticBrowserError(RuntimeError):
    """Inspection failed without exposing page content or retrying."""


@dataclass(frozen=True, slots=True)
class StaticBrowserReceipt:
    binding_digest: str
    source_sha256: str
    title: str
    text: str
    final_url: str
    blocked_request_count: int
    image_id: str


def _input(target: BrowserTarget, html: bytes, css: bytes, now: datetime) -> tuple[bytes, str]:
    try:
        target.check_navigation(target.approved_url, now=now)
    except BrowserTargetError as error:
        raise StaticBrowserError("browser_target_invalid") from error
    if not target.approved_url.startswith("http://127.0.0.1:"):
        raise StaticBrowserError("browser_target_host_unsupported")
    if not isinstance(html, bytes) or not isinstance(css, bytes):
        raise StaticBrowserError("browser_source_invalid")
    if not html or len(html) + len(css) + len(b"<style></style>") > min(target.max_page_bytes, 262_144):
        raise StaticBrowserError("browser_source_bounds_invalid")
    try:
        html_text, css_text = html.decode("utf-8"), css.decode("utf-8")
    except UnicodeError as error:
        raise StaticBrowserError("browser_source_invalid") from error
    if (
        "</style" in css_text.lower()
        or contains_sensitive_text(html_text)
        or contains_sensitive_text(css_text)
    ):
        raise StaticBrowserError("browser_source_rejected")
    source_sha256 = hashlib.sha256(html + b"\x00" + css).hexdigest()
    payload = json.dumps(
        {
            "url": target.approved_url,
            "html": html_text,
            "css": css_text,
            "width": target.viewport_width,
            "height": target.viewport_height,
            "timeout_ms": target.timeout_seconds * 1000,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > 262_144:
        raise StaticBrowserError("browser_source_bounds_invalid")
    return payload, source_sha256


def validate_receipt(
    raw: bytes, target: BrowserTarget, source_sha256: str, image_id: str, *, now: datetime
) -> StaticBrowserReceipt:
    if not isinstance(raw, bytes) or len(raw) > MAX_RECEIPT:
        raise StaticBrowserError("browser_receipt_invalid")
    try:
        value = json.loads(raw)
        target.check_navigation(value["final_url"], now=now)
    except (ValueError, TypeError, KeyError, BrowserTargetError) as error:
        raise StaticBrowserError("browser_receipt_invalid") from error
    if not isinstance(value, dict) or set(value) != {
        "title",
        "text",
        "final_url",
        "status",
        "blocked_request_count",
        "popup_count",
        "download_count",
        "no_default_route",
        "no_host_paths",
        "source_sha256",
    }:
        raise StaticBrowserError("browser_receipt_invalid")
    title, body = value["title"], value["text"]
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or len(title) > MAX_TITLE
        or len(body) > MAX_TEXT
        or contains_sensitive_text(title)
        or contains_sensitive_text(body)
        or type(value["status"]) is not int
        or value["status"] != 200
        or type(value["blocked_request_count"]) is not int
        or not 0 <= value["blocked_request_count"] <= 1000
        or type(value["popup_count"]) is not int
        or value["popup_count"] != 0
        or type(value["download_count"]) is not int
        or value["download_count"] != 0
        or value["no_default_route"] is not True
        or value["no_host_paths"] is not True
        or value["source_sha256"] != source_sha256
    ):
        raise StaticBrowserError("browser_receipt_invalid")
    return StaticBrowserReceipt(
        binding_digest=target.binding_digest,
        source_sha256=source_sha256,
        title=title,
        text=body,
        final_url=target.approved_url,
        blocked_request_count=value["blocked_request_count"],
        image_id=image_id,
    )


def _check_container(info: dict[str, Any], image_id: str) -> None:
    host = info.get("HostConfig", {})
    if (
        info.get("Image") != image_id
        or info.get("Mounts") != []
        or host.get("Binds") not in (None, [])
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("CapDrop") != ["ALL"]
        or "no-new-privileges:true" not in host.get("SecurityOpt", [])
        or host.get("PidsLimit") != 128
        or host.get("Memory") != 768 * 1024 * 1024
        or host.get("NanoCpus") != 1_000_000_000
        or host.get("PortBindings") not in (None, {})
        or info.get("Config", {}).get("User") != "1000:1000"
        or set(host.get("Tmpfs", {})) != {"/tmp"}  # noqa: S108 - container-private tmpfs
    ):
        raise StaticBrowserError("browser_container_boundary_invalid")


def run_static_browser_inspection(
    target: BrowserTarget,
    *,
    html: bytes,
    css: bytes,
    image: str,
    docker: str = "/usr/bin/docker",
    now: datetime | None = None,
) -> StaticBrowserReceipt:
    """Inspect trusted fixture bytes once; return text metadata, never a PNG."""
    checked_at = now or datetime.now(UTC)
    payload, source_sha256 = _input(target, html, css, checked_at)
    if not isinstance(image, str) or _IMAGE.fullmatch(image) is None or not Path(docker).is_file():
        raise StaticBrowserError("browser_config_invalid")
    try:
        if not stat.S_ISSOCK(os.stat("/var/run/docker.sock").st_mode):
            raise StaticBrowserError("docker_socket_unavailable")
    except OSError as error:
        raise StaticBrowserError("docker_socket_unavailable") from error
    try:
        installed = _inspect(docker, "image", image)
        if (
            installed.get("Id") != image
            or installed.get("Config", {}).get("Labels", {}).get("onefoundry.browser.base_digest")
            != BASE_DIGEST
        ):
            raise StaticBrowserError("browser_image_unverified")
        name = f"onefoundry-browser-static-{uuid4().hex[:12]}"
        created = False
        try:
            _docker(
                docker,
                "create",
                "--interactive",
                "--pull=never",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=128",
                "--memory=768m",
                "--cpus=1",
                "--user=1000:1000",
                "--tmpfs",
                "/tmp:rw,nosuid,size=256m",  # noqa: S108 - container-private tmpfs
                "--env",
                "HOME=/tmp",
                "--env",
                "PYTHONPATH=",  # noqa: S108
                "--log-driver=none",
                image,
                "python",
                "/opt/onefoundry/browser_static_child.py",
                timeout=20,
            )
            created = True
            _check_container(_inspect(docker, "container", name), image)
            timeout = min(target.timeout_seconds + 10, target.limits.max_timeout_seconds + 10)
            result = subprocess.run(  # noqa: S603 - fixed Docker argv and verified image/container
                (docker, "--host", "unix:///var/run/docker.sock", "start", "--attach", "--interactive", name),
                input=payload,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
            )
            if result.returncode:
                raise StaticBrowserError("browser_child_failed")
            return validate_receipt(result.stdout, target, source_sha256, image, now=datetime.now(UTC))
        finally:
            if created:
                _remove(docker, "container", name)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StaticBrowserError("browser_docker_unavailable_or_timed_out") from error
