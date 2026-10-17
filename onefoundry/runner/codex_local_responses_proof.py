"""Actual-Docker Codex turn against a disposable local Responses fixture."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from runner import docker_egress_proof as egress

FIXTURE_CHILD = Path(__file__).with_name("codex_local_responses_child.py").resolve()
PROXY_CHILD = egress.CHILD
NODE_PROGRAM = r"""
const fs = require('fs');
const cp = require('child_process');
const net = require('net');
fs.mkdirSync('/workspace', {recursive: true});
fs.writeFileSync('/workspace/README.md', 'Synthetic local proof repository.\n');
const privateHomeWasEmpty = !fs.existsSync(process.env.CODEX_HOME)
  && fs.readdirSync('/home/codex').length === 0;
fs.mkdirSync(process.env.CODEX_HOME, {recursive: true});
function canConnect(ip, port) {
  return new Promise(resolve => {
    const socket = net.createConnection({host: ip, port, timeout: 500});
    socket.once('connect', () => {socket.destroy(); resolve(true);});
    socket.once('error', () => {socket.destroy(); resolve(false);});
    socket.once('timeout', () => {socket.destroy(); resolve(false);});
  });
}
const argv = [
  '--ask-for-approval', 'never',
  '-c', 'model_provider="fixture"',
  '-c', 'model_providers.fixture={name="Local fixture",base_url="https://provider.test/v1",env_key="OPENAI_API_KEY",wire_api="responses",supports_websockets=false,request_max_retries=0,stream_max_retries=0}',
  '-m', 'gpt-5',
  'exec', '--json', '--sandbox', 'read-only', '--cd', '/workspace',
  '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
  'Reply briefly. Do not use tools.'
];
(async () => {
  const fixtureDirectDenied = !(await canConnect(process.env.FIXTURE_IP, 443));
  const routes = fs.readFileSync('/proc/net/route', 'utf8').split('\n').slice(1);
  const noDefaultRoute = routes.every(line => !line || line.trim().split(/\s+/)[1] !== '00000000');
  const noHostPaths = ['/root/.codex', '/home/node/.codex', '/runner-spool',
    '/var/run/docker.sock', '/source-roots', '/app'].every(path => !fs.existsSync(path));
  const result = cp.spawnSync('/opt/codex-cli/node_modules/.bin/codex', argv, {
    cwd: '/workspace', encoding: 'utf8', timeout: 20000, maxBuffer: 65536,
    env: process.env
  });
  process.stdout.write(JSON.stringify({
    exit_code: result.status, signal: result.signal,
    stdout: result.stdout, stderr: result.stderr,
    repo: fs.readFileSync('/workspace/README.md', 'utf8'),
    fixture_direct_denied: fixtureDirectDenied,
    no_default_route: noDefaultRoute,
    no_host_paths: noHostPaths,
    private_home_was_empty: privateHomeWasEmpty
  }));
})().catch(error => {process.stderr.write(String(error)); process.exitCode = 1;});
"""


def _cleanup(containers: list[str], networks: list[str], active_error: BaseException | None) -> None:
    failed: list[str] = []
    for name in reversed(containers):
        try:
            egress._remove("container", name)
        except egress.ProofError:
            failed.append(name)
    for name in reversed(networks):
        try:
            egress._remove("network", name)
        except egress.ProofError:
            failed.append(name)
    if failed:
        cleanup_error = egress.ProofError("proof_cleanup_failed: " + ", ".join(failed))
        if active_error is not None:
            active_error.add_note(str(cleanup_error))
        else:
            raise cleanup_error


def _start_fixture(name: str, network: str, tls_dir: Path, image_id: str, nonce: str) -> None:
    egress._docker(
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
        "--user=0:0",
        "--tmpfs",
        "/app:rw,noexec,nosuid,size=16m",
        "--mount",
        f"type=bind,src={FIXTURE_CHILD},dst=/fixture.py,readonly",
        "--mount",
        f"type=bind,src={PROXY_CHILD},dst=/runner/egress_probe_child.py,readonly",
        "--mount",
        f"type=bind,src={tls_dir},dst=/tls,readonly",
        "--env",
        "PYTHONPATH=/",
        "--env",
        f"PROOF_NONCE={nonce}",
        image_id,
        "/usr/local/bin/python3",
        "/fixture.py",
    )


def _server_certificate(tls_dir: Path) -> None:
    extensions = tls_dir / "leaf.ext"
    extensions.write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:provider.test\n"
    )
    commands = (
        (
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(tls_dir / "leaf-key.pem"),
            "-out",
            str(tls_dir / "leaf.csr"),
            "-subj",
            "/CN=provider.test",
        ),
        (
            "openssl",
            "x509",
            "-req",
            "-in",
            str(tls_dir / "leaf.csr"),
            "-CA",
            str(tls_dir / "cert.pem"),
            "-CAkey",
            str(tls_dir / "key.pem"),
            "-CAcreateserial",
            "-out",
            str(tls_dir / "leaf-cert.pem"),
            "-days",
            "1",
            "-extfile",
            str(extensions),
        ),
    )
    for command in commands:
        result = subprocess.run(  # noqa: S603 - fixed OpenSSL command and local synthetic paths
            command, capture_output=True, timeout=15, check=False
        )
        if result.returncode:
            raise egress.ProofError("local_server_certificate_failed")


def _start_proxy(name: str, network: str, fixture_ip: str, image_id: str) -> None:
    egress._docker(
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
        "--user=0:0",
        "--tmpfs",
        "/app:rw,noexec,nosuid,size=16m",
        "--mount",
        f"type=bind,src={FIXTURE_CHILD},dst=/fixture.py,readonly",
        "--mount",
        f"type=bind,src={PROXY_CHILD},dst=/runner/egress_probe_child.py,readonly",
        "--env",
        "PYTHONPATH=/",
        image_id,
        "/usr/local/bin/python3",
        "/fixture.py",
        "proxy",
        fixture_ip,
    )


def _start_client(name: str, image_id: str, network: str, proxy_ip: str, fixture_ip: str, cert: Path) -> None:
    egress._docker(
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
        "--pids-limit=64",
        "--memory=512m",
        "--memory-swap=512m",
        "--cpus=1",
        "--user=1000:1000",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=16m",
        "--tmpfs",
        "/home/codex:rw,nosuid,nodev,size=32m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",  # noqa: S108 - private tmpfs
        "--mount",
        f"type=bind,src={cert},dst=/etc/ssl/certs/ca-certificates.crt,readonly",
        "--env",
        "HOME=/home/codex",
        "--env",
        "CODEX_HOME=/home/codex/.codex",
        "--env",
        "OPENAI_API_KEY=local-test-only",
        "--env",
        f"HTTPS_PROXY=http://{proxy_ip}:3128",
        "--env",
        f"HTTP_PROXY=http://{proxy_ip}:3128",
        "--env",
        "NO_PROXY=",
        "--env",
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        "CODEX_CA_CERTIFICATE=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        f"FIXTURE_IP={fixture_ip}",
        "--entrypoint",
        "/usr/local/bin/node",
        image_id,
        "-e",
        NODE_PROGRAM,
    )


def prove_local_codex_turn(image_id: str, fixture_image: str) -> dict[str, object]:
    """Return inspectable evidence; fail if the local turn or topology is unproved."""
    egress._inspect("image", image_id)
    fixture_image_id = egress._inspect("image", fixture_image)["Id"]
    if not isinstance(fixture_image_id, str) or not fixture_image_id.startswith("sha256:"):
        raise egress.ProofError("fixture_image_invalid")
    suffix = uuid4().hex[:12]
    nonce = uuid4().hex
    client_net = f"onefoundry-codex-client-{suffix}"
    fixture_net = f"onefoundry-codex-fixture-{suffix}"
    fixture = f"onefoundry-codex-fixture-{suffix}"
    proxy = f"onefoundry-codex-proxy-{suffix}"
    client = f"onefoundry-codex-client-{suffix}"
    networks: list[str] = []
    containers: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-codex-local-proof-") as temporary:
            tls_dir = Path(temporary) / "tls"
            tls_dir.mkdir()
            egress._certificate(tls_dir)
            _server_certificate(tls_dir)
            # Disposable fixture key is synthetic; Docker's mapped UID must read it.
            (tls_dir / "leaf-key.pem").chmod(0o644)
            for network in (client_net, fixture_net):
                networks.append(network)
                egress._network(network)
            containers.append(fixture)
            _start_fixture(fixture, fixture_net, tls_dir, fixture_image_id, nonce)
            try:
                egress._ready(fixture)
            except egress.ProofError as exc:
                state = egress._inspect("container", fixture)["State"]
                stderr = subprocess.run(  # noqa: S603 - fixed Docker executable and unique proof name
                    ("/usr/bin/docker", "logs", fixture), capture_output=True, text=True, check=False
                ).stderr
                raise egress.ProofError(f"fixture_start_failed: {state!r} {stderr[:1000]}") from exc
            fixture_ip = egress._ip(fixture, fixture_net)
            containers.append(proxy)
            _start_proxy(proxy, client_net, fixture_ip, fixture_image_id)
            egress._docker("network", "connect", fixture_net, proxy)
            egress._ready(proxy)
            proxy_ip = egress._ip(proxy, client_net)
            containers.append(client)
            _start_client(client, image_id, client_net, proxy_ip, fixture_ip, tls_dir / "cert.pem")
            inspected = egress._inspect("container", client)
            host = inspected["HostConfig"]
            memberships = inspected["NetworkSettings"]["Networks"]
            mounts = {item["Destination"] for item in inspected["Mounts"]}
            if (
                inspected["Image"] != image_id
                or set(memberships) != {client_net}
                or mounts != {"/etc/ssl/certs/ca-certificates.crt"}
                or host["ReadonlyRootfs"] is not True
                or host["CapDrop"] != ["ALL"]
                or host["SecurityOpt"] != ["no-new-privileges:true"]
                or host["Memory"] != 512 * 1024 * 1024
                or host["MemorySwap"] != 512 * 1024 * 1024
                or host["PidsLimit"] != 64
                or host["NanoCpus"] != 1_000_000_000
                or any(value.startswith("PROOF_NONCE=") for value in inspected["Config"]["Env"])
            ):
                raise egress.ProofError("codex_container_boundary_invalid")
            egress._docker("wait", client, timeout=30)
            output = json.loads(egress._docker("logs", client))
            observations = [json.loads(line) for line in egress._docker("logs", fixture).splitlines()[1:]]
            proxy_events = [json.loads(line) for line in egress._docker("logs", proxy).splitlines()[1:]]
            events = [json.loads(line) for line in output["stdout"].splitlines()]
            thread = next((item for item in events if item.get("type") == "thread.started"), None)
            message = next(
                (
                    item
                    for item in events
                    if item.get("type") == "item.completed"
                    and item.get("item", {}).get("type") == "agent_message"
                ),
                None,
            )
            if (
                output.get("exit_code") != 0
                or output.get("signal") is not None
                or output.get("repo") != "Synthetic local proof repository.\n"
                or not all(
                    output.get(key) is True
                    for key in (
                        "fixture_direct_denied",
                        "no_default_route",
                        "no_host_paths",
                        "private_home_was_empty",
                    )
                )
                or not isinstance(thread, dict)
                or not isinstance(thread.get("thread_id"), str)
                or message is None
                or message["item"].get("text") != f"local-proof-{nonce}"
                or not any(item.get("type") == "turn.completed" for item in events)
                or observations
                != [
                    {
                        "request_line": "POST /v1/responses HTTP/1.1",
                        "authorized": True,
                        "stream": True,
                        "model": "gpt-5",
                    }
                ]
                or sum(item.get("tls_sni") == "provider.test" for item in proxy_events) != 1
                or sum(item.get("connect") == "CONNECT provider.test:443 HTTP/1.1" for item in proxy_events)
                != 1
                or any(
                    item.get("allowed") is True
                    and item.get("connect") != "CONNECT provider.test:443 HTTP/1.1"
                    for item in proxy_events
                )
            ):
                raise egress.ProofError("codex_local_turn_unproved")
            proof = {
                "boundary": {
                    "image_id": inspected["Image"],
                    "networks": sorted(memberships),
                    "mounts": sorted(mounts),
                    "read_only": host["ReadonlyRootfs"],
                    "cap_drop": host["CapDrop"],
                    "security_opt": host["SecurityOpt"],
                    "memory": host["Memory"],
                    "memory_swap": host["MemorySwap"],
                    "pids_limit": host["PidsLimit"],
                    "nano_cpus": host["NanoCpus"],
                    "user": inspected["Config"]["User"],
                },
                "nonce": nonce,
                "cli": output,
                "requests": observations,
                "proxy": proxy_events,
            }
            if "local-test-only" in json.dumps(proof):
                raise egress.ProofError("codex_proof_log_contains_dummy_key")
            return proof
    finally:
        _cleanup(containers, networks, sys.exception())
