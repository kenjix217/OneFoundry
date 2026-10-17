"""Disposable, local-only Docker network proof. Never launches a provider CLI."""

from __future__ import annotations

import json
import os
import re
import selectors
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import uuid4

CHILD = Path(__file__).with_name("egress_probe_child.py").resolve()
GATEWAY_MODE = "com.docker.network.bridge.gateway_mode_ipv4=isolated"
EXPECTED_OBSERVATIONS = frozenset(
    {
        "allowed_tls",
        "other_host_denied",
        "other_port_denied",
        "ip_literal_denied",
        "wrong_sni_denied",
        "http_form_denied",
        "fixture_direct_denied",
        "host_direct_denied",
        "no_default_route",
        "no_host_paths",
        "no_app_source",
        "no_provider_auth",
    }
)


class ProofError(RuntimeError):
    """A required isolation property was unavailable or the proof failed."""


def _docker(*arguments: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed Docker executable, argument list, no shell
            ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *arguments),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofError("docker_command_unavailable") from exc
    if result.returncode:
        raise ProofError(f"docker_{arguments[0]}_failed")
    return result.stdout.strip()


def bounded_logs(
    name: str,
    max_bytes: int,
    *,
    timeout: int = 10,
    follow: bool = False,
    on_stdout_chunk: Callable[[bytes], None] | None = None,
) -> bytes:
    """Read bounded logs, optionally publishing parsed progress while running."""
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", name) is None
        or not 1 <= max_bytes <= 1_000_000
        or not 1 <= timeout <= 3600
    ):
        raise ProofError("docker_log_request_invalid")
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed Docker command and validated name
            (
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "logs",
                *(("--follow",) if follow else ()),
                name,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except OSError as exc:
        raise ProofError("docker_logs_unavailable") from exc
    assert process.stdout is not None and process.stderr is not None
    outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (ready := selector.select(remaining)):
                    raise ProofError("docker_logs_timeout")
                for key, _ in ready:
                    pipe = cast(BinaryIO, key.fileobj)
                    if pipe not in outputs:
                        raise ProofError("docker_logs_pipe_invalid")
                    current = len(outputs[process.stdout]) + len(outputs[process.stderr])
                    chunk = os.read(pipe.fileno(), min(8192, max_bytes + 1 - current))
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    outputs[pipe].extend(chunk)
                    if current + len(chunk) > max_bytes:
                        raise ProofError("docker_logs_limit")
                    if pipe is process.stdout and on_stdout_chunk is not None:
                        try:
                            on_stdout_chunk(chunk)
                        except Exception as exc:
                            raise ProofError("docker_progress_callback_failed") from exc
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode != 0:
            raise ProofError("docker_logs_failed")
        return bytes(outputs[process.stdout]).strip()
    except (ProofError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise ProofError("docker_logs_timeout") from exc
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def _inspect(kind: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_docker("inspect", "--type", kind, name))
    except (ValueError, TypeError) as exc:
        raise ProofError("docker_inspect_invalid") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ProofError("docker_inspect_invalid")
    return value[0]


def _network(name: str) -> None:
    _docker(
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
    value = _inspect("network", name)
    if (
        value.get("Internal") is not True
        or value.get("EnableIPv6") is not False
        or value.get("Driver") != "bridge"
        or value.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
    ):
        raise ProofError("isolated_gateway_unverified")


def _container(
    name: str, image: str, network: str, role: str, arguments: tuple[str, ...], mounts: tuple[str, ...]
) -> None:
    command = [
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
        "--pids-limit=32",
        "--memory=128m",
        "--cpus=1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",  # noqa: S108 - private container tmpfs
        "--tmpfs",
        "/app:rw,noexec,nosuid,size=16m",  # hides the reused test image's application source
        "--mount",
        f"type=bind,src={CHILD},dst=/probe.py,readonly",
    ]
    for mount in mounts:
        command.extend(("--mount", mount))
    command.extend(("--env", "PYTHONPATH=", image, "/usr/local/bin/python3", "/probe.py", role, *arguments))
    _docker(*command)


def _ip(name: str, network: str) -> str:
    value = _inspect("container", name)
    networks = value.get("NetworkSettings", {}).get("Networks", {})
    if not isinstance(networks, dict) or network not in networks:
        raise ProofError("container_network_unverified")
    address = networks[network].get("IPAddress")
    if not isinstance(address, str) or not address:
        raise ProofError("container_address_unavailable")
    return address


def _ready(name: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = _inspect("container", name)
        if value.get("State", {}).get("Running") is not True:
            raise ProofError("proof_service_exited")
        if "READY" in _docker("logs", name):
            return
        time.sleep(0.05)
    raise ProofError("proof_service_not_ready")


def _mounts(name: str, expected: set[str]) -> None:
    value = _inspect("container", name)
    actual = {item.get("Destination") for item in value.get("Mounts", [])}
    if actual != expected:
        raise ProofError("proof_mount_boundary_invalid")


def _certificate(directory: Path) -> None:
    command = (
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-keyout",
        str(directory / "key.pem"),
        "-out",
        str(directory / "cert.pem"),
        "-subj",
        "/CN=provider.test",
        "-addext",
        "subjectAltName=DNS:provider.test",
    )
    try:
        result = subprocess.run(command, capture_output=True, timeout=15, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofError("local_certificate_unavailable") from exc
    if result.returncode:
        raise ProofError("local_certificate_failed")


def _remove(kind: str, name: str) -> None:
    try:
        if kind == "container":
            _docker("rm", "--force", name, timeout=10)
        else:
            _docker("network", "rm", name, timeout=10)
    except ProofError as exc:
        try:
            _inspect(kind, name)
        except ProofError:
            return  # Nothing exists under the unique proof name.
        raise ProofError("proof_cleanup_failed") from exc


def run_local_egress_proof(image: str) -> dict[str, bool]:
    """Check controlled local routes and inspect a restricted Docker topology.

    Both Docker networks are internal with isolated gateway mode. The proxy's
    second network leads only to a local TLS fixture. No provider or Internet
    connection is attempted by the orchestrator.
    """
    if not image or any(char.isspace() for char in image) or not CHILD.is_file():
        raise ProofError("invalid_proof_image_or_child")
    try:
        if not stat.S_ISSOCK(Path("/var/run/docker.sock").stat().st_mode):
            raise ProofError("local_docker_socket_unavailable")
    except OSError as exc:
        raise ProofError("local_docker_socket_unavailable") from exc
    _inspect("image", image)  # Require a preinstalled image; Docker may not pull.
    suffix = uuid4().hex[:12]
    client_net = f"onefoundry-egress-client-{suffix}"
    fixture_net = f"onefoundry-egress-fixture-{suffix}"
    fixture = f"onefoundry-egress-fixture-{suffix}"
    proxy = f"onefoundry-egress-proxy-{suffix}"
    client = f"onefoundry-egress-client-{suffix}"
    created_networks: list[str] = []
    created_containers: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-egress-proof-") as temporary:
            tls_dir = Path(temporary) / "tls"
            tls_dir.mkdir()
            _certificate(tls_dir)
            # The child runs as the host UID and can read only this synthetic test cert.
            created_networks.append(client_net)
            _network(client_net)
            created_networks.append(fixture_net)
            _network(fixture_net)
            created_containers.append(fixture)
            _container(
                fixture,
                image,
                fixture_net,
                "fixture",
                (),
                (f"type=bind,src={tls_dir},dst=/tls,readonly",),
            )
            _mounts(fixture, {"/probe.py", "/tls"})
            _ready(fixture)
            fixture_ip = _ip(fixture, fixture_net)
            created_containers.append(proxy)
            _container(proxy, image, client_net, "proxy", (fixture_ip,), ())
            _mounts(proxy, {"/probe.py"})
            _docker("network", "connect", fixture_net, proxy)
            _ready(proxy)
            proxy_ip = _ip(proxy, client_net)
            # A host-local listener makes the host gateway denial meaningful.
            bridge = _inspect("network", "bridge")
            host_ip = bridge["IPAM"]["Config"][0]["Gateway"]
            if not isinstance(host_ip, str) or not host_ip:
                raise ProofError("host_gateway_unavailable")
            with socket.socket() as host_listener:
                host_listener.bind((host_ip, 0))
                host_listener.listen(1)
                created_containers.append(client)
                _container(
                    client,
                    image,
                    client_net,
                    "client",
                    (proxy_ip, fixture_ip, host_ip, str(host_listener.getsockname()[1])),
                    (f"type=bind,src={tls_dir / 'cert.pem'},dst=/tls/cert.pem,readonly",),
                )
                _mounts(client, {"/probe.py", "/tls/cert.pem"})
                memberships = _inspect("container", client)["NetworkSettings"]["Networks"]
                if set(memberships) != {client_net}:
                    raise ProofError("client_network_membership_invalid")
                _docker("wait", client, timeout=20)
                state = _inspect("container", client)["State"]
                if state.get("ExitCode") != 0:
                    raise ProofError(f"local_egress_client_failed: {_docker('logs', client)[:1000]}")
                try:
                    observations = json.loads(_docker("logs", client))
                except (ValueError, TypeError) as exc:
                    raise ProofError("local_egress_output_invalid") from exc
                if (
                    not isinstance(observations, dict)
                    or set(observations) != EXPECTED_OBSERVATIONS
                    or not all(item is True for item in observations.values())
                ):
                    raise ProofError("local_egress_observation_failed")
                return observations
    finally:
        cleanup_failed = False
        for name in reversed(created_containers):
            try:
                _remove("container", name)
            except ProofError:
                cleanup_failed = True
        for name in reversed(created_networks):
            try:
                _remove("network", name)
            except ProofError:
                cleanup_failed = True
        if cleanup_failed:
            raise ProofError("proof_cleanup_failed")
