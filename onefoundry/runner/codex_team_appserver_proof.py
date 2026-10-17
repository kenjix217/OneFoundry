"""Offline Docker proof of one real Codex app-server dynamic tool turn.

The provider is a disposable local fixture. The child has only a read-only
repository, a placeholder credential, and an internal network through a fixed
broker. The broker receives the dummy provider credential through a pipe.
This proof does not admit a live account, worker, or installed team cutover.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner import codex_local_responses_proof as local
from runner import docker_egress_proof as egress
from runner.codex_app_server import AppServerTimeout, CodexAppServer

FIXTURE = Path(__file__).with_name("codex_team_appserver_fixture.py").resolve()
BROKER = Path(__file__).with_name("codex_team_broker_child.py").resolve()
POLICY = Path(__file__).with_name("codex_team_broker_policy.py").resolve()
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CLIENT_BOUNDARY_PROBE = r"""
const fs=require('fs'),net=require('net');
function connect(ip,port){return new Promise(resolve=>{
  const socket=net.createConnection({host:ip,port,timeout:500});
  socket.once('connect',()=>{socket.destroy();resolve(true)});
  socket.once('error',()=>resolve(false));
  socket.once('timeout',()=>{socket.destroy();resolve(false)});
});}
function denied(ip){return new Promise(resolve=>{
  const socket=net.createConnection({host:ip,port:3128,timeout:500});
  let answer='';
  socket.once('connect',()=>socket.write('CONNECT other.test:443 HTTP/1.1\r\nHost: other.test\r\n\r\n'));
  socket.on('data',part=>{answer+=part.toString();if(answer.includes('\r\n\r\n')){
    socket.destroy();resolve(answer.startsWith('HTTP/1.1 403'))}});
  socket.once('error',()=>resolve(false));
  socket.once('timeout',()=>{socket.destroy();resolve(false)});
});}
(async()=>{
  const routes=fs.readFileSync('/proc/net/route','utf8').split('\n').slice(1);
  const noDefaultRoute=routes.every(line=>!line||line.trim().split(/\s+/)[1]!=='00000000');
  const noHostPaths=['/broker-secret','/runner-spool','/var/run/docker.sock','/run/secrets','/app']
    .every(path=>!fs.existsSync(path));
  const fixtureDirectDenied=!(await connect(process.argv[1],443));
  const otherHostDenied=await denied(process.argv[2]);
  process.stdout.write(JSON.stringify({noDefaultRoute,noHostPaths,fixtureDirectDenied,otherHostDenied}));
})().catch(()=>process.exit(2));
"""


def _start_fixture(name: str, network: str, tls: Path, image: str, nonce: str, request_path: str) -> None:
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
        f"type=bind,src={FIXTURE},dst=/fixture.py,readonly",
        "--mount",
        f"type=bind,src={local.PROXY_CHILD},dst=/runner/egress_probe_child.py,readonly",
        "--mount",
        f"type=bind,src={local.FIXTURE_CHILD},dst=/runner/codex_local_responses_child.py,readonly",
        "--mount",
        f"type=bind,src={tls},dst=/tls,readonly",
        "--env",
        "PYTHONPATH=/",
        "--env",
        f"PROOF_NONCE={nonce}",
        "--env",
        f"PROOF_REQUEST_PATH={request_path}",
        image,
        "/usr/local/bin/python3",
        "/fixture.py",
    )


def _start_broker(
    name: str, network: str, fixture_ip: str, tls: Path, image: str, request_path: str
) -> subprocess.Popen[bytes]:
    command = [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "run",
        "-i",
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
        "--user=1000:1000",
        "--tmpfs",
        "/app:rw,noexec,nosuid,size=16m",
        "--mount",
        f"type=bind,src={BROKER},dst=/runner/codex_team_broker_child.py,readonly",
        "--mount",
        f"type=bind,src={POLICY},dst=/runner/codex_team_broker_policy.py,readonly",
        "--mount",
        f"type=bind,src={tls},dst=/tls,readonly",
        "--env",
        "PYTHONPATH=/",
        image,
        "/usr/local/bin/python3",
        "/runner/codex_team_broker_child.py",
        "--host",
        "provider.test",
        "--request-path",
        request_path,
        "--upstream",
        fixture_ip,
        "--model",
        "gpt-5",
        "--max-request-bytes",
        "131072",
        "--max-requests",
        "4",
        "--max-response-bytes",
        "65536",
        "--upstream-ca-file",
        "/tls/cert.pem",
    ]
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed admitted Docker command, no shell
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
        assert process.stdin is not None
        process.stdin.write(
            b"local-test-only\nfixture-account\n"
            if request_path == "/backend-api/codex/responses"
            else b"local-test-only\n"
        )
        process.stdin.flush()
    except (OSError, BrokenPipeError) as exc:
        raise egress.ProofError("team_broker_start_failed") from exc
    return process


def _check_broker(name: str, image: str, client_net: str, fixture_net: str, tls: Path) -> None:
    value = egress._inspect("container", name)
    host = value["HostConfig"]
    mounts = {entry["Destination"]: entry for entry in value["Mounts"]}
    if (
        value["Image"] != image
        or set(value["NetworkSettings"]["Networks"]) != {client_net, fixture_net}
        or set(mounts)
        != {"/runner/codex_team_broker_child.py", "/runner/codex_team_broker_policy.py", "/tls"}
        or any(entry["RW"] for entry in mounts.values())
        or mounts["/tls"]["Source"] != str(tls)
        or host["ReadonlyRootfs"] is not True
        or host["Privileged"] is not False
        or "ALL" not in host["CapDrop"]
        or "no-new-privileges:true" not in host["SecurityOpt"]
        or any("local-test-only" in entry for entry in value["Config"]["Env"])
    ):
        raise egress.ProofError("team_broker_boundary_invalid")


def _wait_broker_created(name: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise egress.ProofError("team_broker_exited_before_admission")
        try:
            egress._inspect("container", name)
            return
        except egress.ProofError:
            time.sleep(0.05)
    raise egress.ProofError("team_broker_not_created")


def _client_argv(
    name: str,
    image: str,
    network: str,
    proxy: str,
    repo: Path,
    home: Path,
    cert: Path,
    request_path: str,
) -> list[str]:
    provider_base = (
        "https://provider.test/backend-api/codex"
        if request_path == "/backend-api/codex/responses"
        else "https://provider.test/v1"
    )
    provider_config = (
        'model_providers.fixture={name="Local fixture",base_url="'
        + provider_base
        + '",env_key="OPENAI_API_KEY",wire_api="responses",'
        "supports_websockets=false,request_max_retries=0,stream_max_retries=0}"
    )
    return [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "run",
        "-i",
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
        "/tmp:rw,nosuid,nodev,size=16m",  # noqa: S108 - private tmpfs
        "--mount",
        f"type=bind,src={home},dst=/home/codex",
        "--mount",
        f"type=bind,src={repo},dst={repo},readonly",
        "--mount",
        f"type=bind,src={cert},dst=/etc/ssl/certs/ca-certificates.crt,readonly",
        "--env",
        "HOME=/home/codex",
        "--env",
        "CODEX_HOME=/home/codex",
        "--env",
        "OPENAI_API_KEY=client-placeholder-only",
        "--env",
        f"HTTPS_PROXY=http://{proxy}:3128",
        "--env",
        f"HTTP_PROXY=http://{proxy}:3128",
        "--env",
        "NO_PROXY=",
        "--env",
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        "CODEX_CA_CERTIFICATE=/etc/ssl/certs/ca-certificates.crt",
        "--entrypoint",
        "/opt/codex-cli/node_modules/.bin/codex",
        image,
        "--ask-for-approval",
        "never",
        "--disable",
        "shell_tool",
        "--disable",
        "multi_agent",
        "--disable",
        "goals",
        "--disable",
        "view_image",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "remote_plugin",
        "--disable",
        "plugin_sharing",
        "--disable",
        "browser_use",
        "--disable",
        "image_generation",
        "--disable",
        "worktrees",
        "-c",
        'web_search="disabled"',
        "-c",
        'model_provider="fixture"',
        "-c",
        provider_config,
        "app-server",
        "--listen",
        "stdio://",
    ]


def _check_client(
    name: str,
    image: str,
    network: str,
    repo: Path,
    home: Path,
    cert: Path,
    fixture_ip: str,
    broker_ip: str,
) -> None:
    value = egress._inspect("container", name)
    host = value["HostConfig"]
    mounts = {entry["Destination"]: entry for entry in value["Mounts"]}
    if (
        value["Image"] != image
        or set(value["NetworkSettings"]["Networks"]) != {network}
        or set(mounts) != {str(repo), "/home/codex", "/etc/ssl/certs/ca-certificates.crt"}
        or mounts["/home/codex"]["RW"] is not True
        or any(entry["RW"] for path, entry in mounts.items() if path != "/home/codex")
        or mounts[str(repo)]["Source"] != str(repo)
        or mounts["/home/codex"]["Source"] != str(home)
        or mounts["/etc/ssl/certs/ca-certificates.crt"]["Source"] != str(cert)
        or host["ReadonlyRootfs"] is not True
        or host["Privileged"] is not False
        or "ALL" not in host["CapDrop"]
        or "no-new-privileges:true" not in host["SecurityOpt"]
        or any("local-test-only" in entry for entry in value["Config"]["Env"])
    ):
        raise egress.ProofError("team_appserver_client_boundary_invalid")
    observed = json.loads(
        egress._docker(
            "exec",
            name,
            "/usr/local/bin/node",
            "-e",
            CLIENT_BOUNDARY_PROBE,
            fixture_ip,
            broker_ip,
        )
    )
    if observed != {
        "noDefaultRoute": True,
        "noHostPaths": True,
        "fixtureDirectDenied": True,
        "otherHostDenied": True,
    }:
        raise egress.ProofError("team_appserver_network_boundary_invalid")


def prove_team_appserver_turn(
    image: str, fixture_image: str, *, request_path: str = "/v1/responses"
) -> dict[str, Any]:
    """Run a real app-server turn against a fake provider and inspect isolation."""
    if not IMAGE_ID.fullmatch(image) or not IMAGE_ID.fullmatch(fixture_image):
        raise egress.ProofError("team_appserver_image_unpinned")
    if request_path not in {"/v1/responses", "/backend-api/codex/responses"}:
        raise egress.ProofError("team_appserver_provider_path_invalid")
    egress._inspect("image", image)
    egress._inspect("image", fixture_image)
    suffix, nonce = uuid4().hex[:12], uuid4().hex
    client_net, fixture_net = f"team-as-client-{suffix}", f"team-as-fixture-{suffix}"
    fixture, proxy, client_name = (
        f"team-as-provider-{suffix}",
        f"team-as-proxy-{suffix}",
        f"team-as-client-{suffix}",
    )
    containers: list[str] = []
    networks: list[str] = []
    broker_process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-team-as-") as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            tls = root / "tls"
            tls.mkdir(mode=0o755)
            egress._certificate(tls)
            local._server_certificate(tls)
            (tls / "leaf-key.pem").chmod(0o644)
            repo = root / "repo"
            repo.mkdir(mode=0o755)
            (repo / "README.md").write_text("Disposable team protocol proof.\n")
            home = root / "codex-home"
            home.mkdir(mode=0o700)
            for name in (client_net, fixture_net):
                networks.append(name)
                egress._network(name)
            containers.append(fixture)
            _start_fixture(fixture, fixture_net, tls, fixture_image, nonce, request_path)
            egress._ready(fixture)
            fixture_ip = egress._ip(fixture, fixture_net)
            containers.append(proxy)
            broker_process = _start_broker(proxy, client_net, fixture_ip, tls, fixture_image, request_path)
            _wait_broker_created(proxy, broker_process)
            egress._ready(proxy)
            egress._docker("network", "connect", fixture_net, proxy)
            _check_broker(proxy, fixture_image, client_net, fixture_net, tls)
            broker_ip = egress._ip(proxy, client_net)
            observed: list[tuple[str, dict[str, Any], str]] = []

            def invoke(name: str, args: Mapping[str, Any], call_id: str) -> dict[str, Any]:
                observed.append((name, dict(args), call_id))
                return {"success": True, "contentItems": [{"type": "inputText", "text": "saved"}]}

            specifications = [
                {
                    "type": "namespace",
                    "name": "onefoundry",
                    "description": "Saved actions",
                    "tools": [
                        {
                            "type": "function",
                            "name": "publish_update",
                            "description": "Save a public update",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"body": {"type": "string"}},
                                "required": ["body"],
                                "additionalProperties": False,
                            },
                        }
                    ],
                }
            ]
            thread_id: str | None = None
            for process_number in (1, 2):
                name = f"{client_name}-{process_number}"
                argv = _client_argv(
                    name, image, client_net, broker_ip, repo, home, tls / "cert.pem", request_path
                )
                containers.append(name)
                with CodexAppServer(
                    argv,
                    cwd=repo,
                    environment={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
                    dynamic_tool_callback=invoke,
                    timeout_seconds=12,
                ) as server:
                    _check_client(
                        name, image, client_net, repo, home, tls / "cert.pem", fixture_ip, broker_ip
                    )
                    if thread_id is None:
                        thread_id = server.start_thread(
                            repository=repo, model="gpt-5", dynamic_tools=specifications
                        )
                    else:
                        server.resume_thread(
                            thread_id,
                            repository=repo,
                            admitted_dynamic_tools=["onefoundry.publish_update"],
                        )
                    turn_id = server.start_turn("Save one public plan, then finish.", repository=repo)
                    terminal = None
                    for _ in range(100):
                        try:
                            event = server.next_event(timeout_seconds=12)
                        except AppServerTimeout as exc:
                            raise egress.ProofError("team_appserver_event_timeout") from exc
                        if event.get("method") == "turn/completed":
                            terminal = event
                            break
                    if terminal is None:
                        raise egress.ProofError("team_appserver_terminal_missing")
                    params = terminal.get("params")
                    turn = params.get("turn") if isinstance(params, dict) else None
                    if (
                        not isinstance(turn, dict)
                        or turn.get("status") != "completed"
                        or not isinstance(params, dict)
                        or params.get("threadId") != thread_id
                        or turn.get("id") != turn_id
                    ):
                        raise egress.ProofError("team_appserver_terminal_invalid")
            observations = [json.loads(line) for line in egress._docker("logs", fixture).splitlines()[1:]]
            broker_log = egress._docker("logs", proxy)
            broker_events = [json.loads(line) for line in broker_log.splitlines()[1:]]
            forwarded = [item for item in broker_events if item.get("type") == "team_broker.forwarded"]
            if (
                len(observed) != 2
                or any(
                    item
                    != ("onefoundry.publish_update", {"body": f"public plan {nonce}"}, "call_team_fixture")
                    for item in observed
                )
                or len(observations) != 4
                or observations[0].get("completed") is not False
                or observations[1].get("completed") is not True
                or observations[2].get("completed") is not False
                or observations[3].get("completed") is not True
                or observations[0].get("nested_names") != ["publish_update"]
                or observations[0].get("tool_names") != ["onefoundry"]
                or observations[0].get("tool_types") != ["namespace"]
                or len(forwarded) != 4
                or any(
                    item.get("status") != 200 or item.get("native_prompt_removed") is not True
                    for item in forwarded
                )
                or b"local-test-only" in broker_log.encode()
            ):
                raise egress.ProofError("team_appserver_dynamic_action_unproved")
            for saved in home.rglob("*"):
                if saved.is_symlink():
                    raise egress.ProofError("team_appserver_rollout_symlink")
                if saved.is_file():
                    if saved.name == "auth.json" or saved.stat().st_size > 256_000_000:
                        raise egress.ProofError("team_appserver_rollout_secret_or_size")
                    if (
                        b"local-test-only" in saved.read_bytes()
                        or b"client-placeholder-only" in saved.read_bytes()
                    ):
                        raise egress.ProofError("team_appserver_rollout_secret_or_size")
            return {
                "thread_started": True,
                "turn_completed": True,
                "dynamic_calls": len(observed),
                "provider_requests": len(observations),
                "resumed_thread": True,
                "network_isolated": True,
                "provider_tool_names": observations[0]["tool_names"],
                "provider_tool_types": observations[0]["tool_types"],
                "broker_credential_absent_from_child": True,
                "native_prompt_removed": True,
            }
    finally:
        try:
            local._cleanup(containers, networks, sys.exception())
        finally:
            if broker_process is not None:
                if broker_process.stdin is not None:
                    broker_process.stdin.close()
                try:
                    broker_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    broker_process.kill()
                    broker_process.wait(timeout=5)
