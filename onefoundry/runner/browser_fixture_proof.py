"""Host-only Docker supervisor for one OFFLINE SYNTHETIC browser fixture.

This has no OneFoundry Gateway registration. The Docker socket remains on the
host; neither child receives source, credentials, artifact spool, or a mount.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner.browser_fixture_child import MARKER, MAX_PNG

BASE_DIGEST = "sha256:80fd7c1aad9600ea348572dd46ca00b9ea31d890831f5838fc61319ab79900d2"
GATEWAY_MODE = "com.docker.network.bridge.gateway_mode_ipv4=isolated"
URL = "http://127.0.0.1:8765/fixture.html"
MAX_LOG = 800_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BrowserFixtureError(RuntimeError):
    """The synthetic proof could not establish its required result."""


@dataclass(frozen=True)
class BrowserFixtureReceipt:
    marker: str
    title: str
    text: str
    final_url: str
    png_sha256: str
    png: bytes
    no_default_route: bool
    no_host_paths: bool
    hostile_request_blocked: bool
    popup_count: int
    download_count: int
    image_id: str


def validate_child_receipt(raw: str, image_id: str) -> BrowserFixtureReceipt:
    if len(raw.encode("utf-8")) > MAX_LOG:
        raise BrowserFixtureError("browser_log_too_large")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "marker",
            "title",
            "text",
            "final_url",
            "png_sha256",
            "png_base64",
            "no_default_route",
            "no_host_paths",
            "hostile_request_blocked",
            "popup_count",
            "download_count",
        }:
            raise ValueError("shape")
        png = base64.b64decode(value["png_base64"], validate=True)
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise BrowserFixtureError("browser_receipt_invalid") from None
    if (
        value["marker"] != MARKER
        or value["title"] != MARKER
        or value["text"] != MARKER
        or value["final_url"] != URL
        or type(value["no_default_route"]) is not bool
        or not value["no_default_route"]
        or type(value["no_host_paths"]) is not bool
        or not value["no_host_paths"]
        or type(value["hostile_request_blocked"]) is not bool
        or not value["hostile_request_blocked"]
        or type(value["popup_count"]) is not int
        or value["popup_count"] != 0
        or type(value["download_count"]) is not int
        or value["download_count"] != 0
        or not (PNG_SIGNATURE == png[:8] and png[-8:] == b"IEND\xaeB`\x82")
        or not (0 < len(png) <= MAX_PNG)
        or value["png_sha256"] != hashlib.sha256(png).hexdigest()
    ):
        raise BrowserFixtureError("browser_receipt_invalid")
    return BrowserFixtureReceipt(
        marker=MARKER,
        title=MARKER,
        text=MARKER,
        final_url=URL,
        png_sha256=value["png_sha256"],
        png=png,
        no_default_route=True,
        no_host_paths=True,
        hostile_request_blocked=True,
        popup_count=0,
        download_count=0,
        image_id=image_id,
    )


def _docker_result(binary: str, *arguments: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed host Docker CLI with argv, no shell
            (binary, "--host", "unix:///var/run/docker.sock", *arguments),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrowserFixtureError("docker_unavailable_or_timed_out") from error
    return result


def _docker(binary: str, *arguments: str, timeout: int = 20) -> str:
    result = _docker_result(binary, *arguments, timeout=timeout)
    if result.returncode:
        raise BrowserFixtureError(f"docker_{arguments[0]}_failed")
    return result.stdout.strip()


def _inspect(binary: str, kind: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_docker(binary, "inspect", "--type", kind, name))
    except (ValueError, TypeError) as error:
        raise BrowserFixtureError("docker_inspect_invalid") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BrowserFixtureError("docker_inspect_invalid")
    return value[0]


def _inspect_optional(binary: str, kind: str, name: str) -> dict[str, Any] | None:
    """Return None only for Docker's exact NotFound response for this resource."""
    if kind not in ("container", "network"):
        raise BrowserFixtureError("browser_cleanup_kind_invalid")
    result = _docker_result(binary, "inspect", "--type", kind, name, timeout=10)
    if result.returncode:
        absent = (
            f"Error response from daemon: No such container: {name}"
            if kind == "container"
            else f"Error response from daemon: network {name} not found"
        )
        if result.returncode == 1 and result.stdout.strip() == "[]" and result.stderr.strip() == absent:
            return None
        raise BrowserFixtureError("browser_cleanup_inspect_failed")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise BrowserFixtureError("browser_cleanup_inspect_invalid") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise BrowserFixtureError("browser_cleanup_inspect_invalid")
    return value[0]


def _network(binary: str, name: str) -> None:
    _docker(
        binary,
        "network",
        "create",
        "--driver",
        "bridge",
        "--internal",
        "--ipv6=false",
        "-o",
        GATEWAY_MODE,
        name,
    )
    value = _inspect(binary, "network", name)
    if (
        value.get("Internal") is not True
        or value.get("EnableIPv6") is not False
        or value.get("Driver") != "bridge"
        or value.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
    ):
        raise BrowserFixtureError("browser_network_invalid")


def _container(binary: str, name: str, image: str, network: str, role: str, image_id: str) -> None:
    expected_network = network
    if network.startswith("container:"):
        fixture_id = _inspect(binary, "container", network.removeprefix("container:")).get("Id")
        if not isinstance(fixture_id, str) or len(fixture_id) != 64:
            raise BrowserFixtureError("fixture_container_id_invalid")
        expected_network = f"container:{fixture_id}"
    _docker(
        binary,
        "run",
        "--detach",
        "--pull=never",
        "--name",
        name,
        "--network",
        network,
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
        "PYTHONPATH=",
        "--log-driver=json-file",
        "--log-opt=max-size=1m",
        "--log-opt=max-file=1",
        image,
        "python",
        "/opt/onefoundry/browser_fixture_child.py",
        role,
        *((URL,) if role == "browser" else ()),
        timeout=25,
    )
    value = _inspect(binary, "container", name)
    host = value.get("HostConfig", {})
    if (
        value.get("Image") != image_id
        or value.get("Mounts") != []
        or host.get("Binds") not in (None, [])
        or host.get("NetworkMode") != expected_network
        or host.get("Privileged") is not False
        or host.get("PidMode") not in ("", None)
        or host.get("IpcMode") == "host"
        or host.get("PortBindings") not in (None, {})
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or "no-new-privileges:true" not in host.get("SecurityOpt", [])
        or host.get("PidsLimit") != 128
        or host.get("Memory") != 768 * 1024 * 1024
        or host.get("NanoCpus") != 1_000_000_000
        or value.get("Config", {}).get("User") != "1000:1000"
        or set(host.get("Tmpfs", {})) != {"/tmp"}  # noqa: S108 - expected container-private tmpfs
    ):
        raise BrowserFixtureError("browser_container_boundary_invalid")


def _ready(binary: str, fixture: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = _inspect(binary, "container", fixture).get("State", {})
        if state.get("Running") is not True:
            raise BrowserFixtureError("browser_fixture_exited")
        if _docker(binary, "logs", "--tail", "1", fixture) == "READY":
            return
        time.sleep(0.05)
    raise BrowserFixtureError("browser_fixture_not_ready")


def _remove(binary: str, kind: str, name: str) -> None:
    removal_error: BrowserFixtureError | None = None
    try:
        if kind == "container":
            _docker(binary, "rm", "--force", name, timeout=10)
        elif kind == "network":
            _docker(binary, "network", "rm", name, timeout=10)
        else:
            raise BrowserFixtureError("browser_cleanup_kind_invalid")
    except BrowserFixtureError as error:
        removal_error = error
    try:
        surviving = _inspect_optional(binary, kind, name)
    except BrowserFixtureError as error:
        raise BrowserFixtureError("browser_cleanup_unverified") from error
    if surviving is not None:
        raise BrowserFixtureError("browser_cleanup_failed") from removal_error


def run_browser_fixture_proof(image: str, *, docker: str = "/usr/bin/docker") -> BrowserFixtureReceipt:
    """Run one fixed local page and return validated in-memory PNG and receipt."""
    if (
        not image.startswith("sha256:")
        or len(image) != 71
        or any(char not in "0123456789abcdef" for char in image[7:])
        or not Path(docker).is_file()
    ):
        raise BrowserFixtureError("browser_config_invalid")
    try:
        if not stat.S_ISSOCK(os.stat("/var/run/docker.sock").st_mode):
            raise BrowserFixtureError("docker_socket_unavailable")
    except OSError as error:
        raise BrowserFixtureError("docker_socket_unavailable") from error
    installed = _inspect(docker, "image", image)
    image_id = installed.get("Id")
    if (
        image_id != image
        or installed.get("Config", {}).get("Labels", {}).get("onefoundry.browser.base_digest") != BASE_DIGEST
    ):
        raise BrowserFixtureError("browser_image_unverified")
    suffix = uuid4().hex[:12]
    network = f"onefoundry-browser-{suffix}"
    fixture = f"onefoundry-browser-fixture-{suffix}"
    browser = f"onefoundry-browser-child-{suffix}"
    created: list[tuple[str, str]] = []
    try:
        created.append(("network", network))
        _network(docker, network)
        created.append(("container", fixture))
        _container(docker, fixture, image, network, "fixture", image_id)
        _ready(docker, fixture)
        fixture_info = _inspect(docker, "container", fixture)
        attached = fixture_info.get("NetworkSettings", {}).get("Networks", {})
        if set(attached) != {network}:
            raise BrowserFixtureError("fixture_network_attachment_invalid")
        created.append(("container", browser))
        _container(docker, browser, image, f"container:{fixture}", "browser", image_id)
        if _docker(docker, "wait", browser, timeout=35) != "0":
            raise BrowserFixtureError("browser_child_failed")
        raw = _docker(docker, "logs", "--tail", "1", browser)
        receipt = validate_child_receipt(raw, image_id)
        return receipt
    finally:
        for kind, name in reversed(created):
            _remove(docker, kind, name)
