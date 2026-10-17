"""One disposable Codex turn through a local, credential holding TLS broker.

This is an offline admission experiment, not a runner entrypoint or live provider
capability. Every Docker network is internal and the only upstream is a fake TLS
Responses server. The broker's dummy key is absent from the coding container.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from runner import codex_local_responses_proof as local
from runner import codex_workorder_fixture as workorder
from runner import docker_egress_proof as egress
from runner.external_cli import _parse_events
from runner.fixture_git import Deadline, _new_repo, _test

BROKER_CHILD = Path(__file__).with_name("codex_broker_child.py").resolve()
BROKER_POLICY = Path(__file__).with_name("codex_broker_policy.py").resolve()
PINNED_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
PROMPT = "Repair calculator.py so add_one returns value + 1. Run no additional commands.\n"
CLIENT_BOOT = r"""
const fs = require('fs');
const cp = require('child_process');
const net = require('net');
function connect(ip, port) {
  return new Promise(resolve => {
    const socket = net.createConnection({host:ip, port, timeout:500});
    socket.once('connect', () => {socket.destroy(); resolve(true);});
    socket.once('error', () => {socket.destroy(); resolve(false);});
    socket.once('timeout', () => {socket.destroy(); resolve(false);});
  });
}
function deniedConnect(target) {
  return new Promise(resolve => {
    const socket = net.createConnection({host:process.env.BROKER_IP, port:3128, timeout:1000});
    let answer = '';
    socket.once('connect', () => socket.write(
      'CONNECT ' + target + ' HTTP/1.1\r\nHost: ' + target + '\r\n\r\n'));
    socket.on('data', data => {
      answer += data.toString();
      if (answer.includes('\r\n\r\n')) {
        socket.destroy();
        resolve(answer.startsWith('HTTP/1.1 403'));
      }
    });
    socket.once('error', () => resolve(false));
    socket.once('timeout', () => {socket.destroy(); resolve(false);});
  });
}
(async () => {
  const homeEmpty = !fs.existsSync(process.env.CODEX_HOME)
    && fs.readdirSync('/home/codex').length === 0;
  const fixtureDirectDenied = !(await connect(process.env.FIXTURE_IP, 443));
  const routes = fs.readFileSync('/proc/net/route', 'utf8').split('\n').slice(1);
  const noDefaultRoute = routes.every(line => !line || line.trim().split(/\s+/)[1] !== '00000000');
  const noHostPaths = ['/broker-secret','/root/.codex','/home/node/.codex','/runner-spool',
    '/var/run/docker.sock','/run/secrets','/app'].every(path => !fs.existsSync(path));
  const otherHostDenied = await deniedConnect('other.test:443');
  const otherPortDenied = await deniedConnect('provider.test:80');
  process.stdout.write(JSON.stringify({type:'boundary.proof',homeEmpty,fixtureDirectDenied,
    noDefaultRoute,noHostPaths,otherHostDenied,otherPortDenied}) + '\n');
  if (![homeEmpty,fixtureDirectDenied,noDefaultRoute,noHostPaths,
    otherHostDenied,otherPortDenied].every(Boolean)) process.exit(2);
  fs.mkdirSync(process.env.CODEX_HOME, {recursive:true});
  const result = cp.spawnSync('/opt/codex-cli/node_modules/.bin/codex', process.argv.slice(1), {
    cwd:'/workspace',env:process.env,input:fs.readFileSync('/prompt.txt','utf8'),
    encoding:'utf8',timeout:20000,maxBuffer:65536
  });
  process.stdout.write(result.stdout || '');
  process.stderr.write(result.stderr || '');
  process.exit(result.status === null ? 1 : result.status);
})().catch(() => process.exit(2));
"""


def _proof_users() -> tuple[str, str]:
    """The broker owns the private dummy key; the Codex child keeps UID 1000."""
    uid, gid = os.geteuid(), os.getegid()
    if uid == 0 or gid == 0:
        raise egress.ProofError("broker_supervisor_must_be_nonroot")
    return f"{uid}:{gid}", f"1000:{gid}"


def _share_disposable_client_mounts(repo: Path, editable: Path, prompt: Path) -> None:
    """Give the Codex child's group access only to its disposable repo and prompt."""
    try:
        repo_info, file_info, prompt_info = repo.lstat(), editable.lstat(), prompt.lstat()
        if (
            not stat.S_ISDIR(repo_info.st_mode)
            or not stat.S_ISREG(file_info.st_mode)
            or not stat.S_ISREG(prompt_info.st_mode)
            or editable.parent != repo
            or any(
                info.st_uid != os.geteuid() or info.st_gid != os.getegid()
                for info in (repo_info, file_info, prompt_info)
            )
        ):
            raise egress.ProofError("broker_client_mount_identity_invalid")
        repo.chmod(0o770)
        editable.chmod(0o660)
        prompt.chmod(0o640)
    except OSError as exc:
        raise egress.ProofError("broker_client_mount_access_unavailable") from exc


def _start_broker(
    name: str, network: str, fixture_ip: str, tls: Path, secret: Path, image: str, user: str
) -> None:
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
        f"--user={user}",
        "--tmpfs",
        "/app:rw,noexec,nosuid,size=16m",
        "--mount",
        f"type=bind,src={BROKER_CHILD},dst=/runner/codex_broker_child.py,readonly",
        "--mount",
        f"type=bind,src={BROKER_POLICY},dst=/runner/codex_broker_policy.py,readonly",
        "--mount",
        f"type=bind,src={tls},dst=/tls,readonly",
        "--mount",
        f"type=bind,src={secret},dst=/broker-secret,readonly",
        "--env",
        "PYTHONPATH=/",
        image,
        "/usr/local/bin/python3",
        "/runner/codex_broker_child.py",
        fixture_ip,
    )


def _start_client(
    name: str,
    network: str,
    image: str,
    broker_ip: str,
    fixture_ip: str,
    repo: Path,
    cert: Path,
    prompt: Path,
    user: str,
) -> None:
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
        f"--user={user}",
        "--tmpfs",
        "/home/codex:rw,nosuid,nodev,size=32m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",  # noqa: S108 - private tmpfs
        "--mount",
        f"type=bind,src={repo},dst=/workspace",
        "--mount",
        f"type=bind,src={cert},dst=/etc/ssl/certs/ca-certificates.crt,readonly",
        "--mount",
        f"type=bind,src={prompt},dst=/prompt.txt,readonly",
        "--env",
        "HOME=/home/codex",
        "--env",
        "CODEX_HOME=/home/codex/.codex",
        "--env",
        "OPENAI_API_KEY=client-placeholder-only",
        "--env",
        f"HTTPS_PROXY=http://{broker_ip}:3128",
        "--env",
        f"HTTP_PROXY=http://{broker_ip}:3128",
        "--env",
        "NO_PROXY=",
        "--env",
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        "CODEX_CA_CERTIFICATE=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        f"BROKER_IP={broker_ip}",
        "--env",
        f"FIXTURE_IP={fixture_ip}",
        "--entrypoint",
        "/usr/local/bin/node",
        image,
        "-e",
        CLIENT_BOOT,
        "--",
        "--ask-for-approval",
        "never",
        "-c",
        'model_provider="fixture"',
        "-c",
        'model_providers.fixture={name="Local fixture",base_url="https://provider.test/v1",env_key="OPENAI_API_KEY",wire_api="responses",supports_websockets=false,request_max_retries=0,stream_max_retries=0}',
        "-m",
        "gpt-5",
        "exec",
        "--json",
        "--sandbox",
        "danger-full-access",
        "--cd",
        "/workspace",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-",
    )


def _check_mounts(
    name: str, expected: dict[str, tuple[Path, bool]], image: str, networks: set[str], user: str
) -> None:
    inspected = egress._inspect("container", name)
    mounts = inspected["Mounts"]
    host = inspected["HostConfig"]
    if (
        inspected["Image"] != image
        or set(inspected["NetworkSettings"]["Networks"]) != networks
        or {item["Destination"] for item in mounts} != set(expected)
        or any(
            item["Source"] != str(expected[item["Destination"]][0])
            or item["RW"] is not expected[item["Destination"]][1]
            for item in mounts
        )
        or host["ReadonlyRootfs"] is not True
        or host["CapDrop"] != ["ALL"]
        or host["SecurityOpt"] != ["no-new-privileges:true"]
        or host["Privileged"] is not False
        or inspected["Config"]["User"] != user
    ):
        raise egress.ProofError("broker_topology_invalid")


def prove_broker_turn(image: str, fixture_image: str) -> dict[str, object]:
    """Prove local credential separation and one bounded, exact Codex repair."""
    if not PINNED_IMAGE.fullmatch(image) or not PINNED_IMAGE.fullmatch(fixture_image):
        raise egress.ProofError("broker_image_unpinned")
    broker_user, client_user = _proof_users()
    egress._inspect("image", image)
    egress._inspect("image", fixture_image)
    suffix = uuid4().hex[:12]
    nonce = uuid4().hex
    client_net, fixture_net = f"ap-broker-client-{suffix}", f"ap-broker-fixture-{suffix}"
    fixture, broker, client = f"ap-broker-provider-{suffix}", f"ap-broker-{suffix}", f"ap-broker-cli-{suffix}"
    containers: list[str] = []
    networks: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-broker-proof-") as temporary:
            root = Path(temporary)
            root.chmod(0o711)
            tls = root / "tls"
            tls.mkdir(mode=0o755)
            egress._certificate(tls)
            local._server_certificate(tls)
            # Root inside Docker may be remapped; this key is local synthetic TLS only.
            (tls / "leaf-key.pem").chmod(0o644)
            secret = root / "broker-secret"
            secret.write_bytes(b"local-test-only")
            secret.chmod(0o600)
            prompt = root / "prompt.txt"
            prompt.write_text(PROMPT)
            prompt.chmod(0o600)
            repo = root / "repo"
            _new_repo(repo, Deadline(15))
            before, _ = _test(repo, Deadline(10), 16384, "before")
            if before["status"] != "failed":
                raise egress.ProofError("broker_baseline_not_failing")
            _share_disposable_client_mounts(repo, repo / "calculator.py", prompt)
            for network in (client_net, fixture_net):
                networks.append(network)
                egress._network(network)
            containers.append(fixture)
            workorder._start_fixture(
                fixture, fixture_net, tls, fixture_image, nonce, sha256(PROMPT.encode()).hexdigest()
            )
            egress._ready(fixture)
            fixture_ip = egress._ip(fixture, fixture_net)
            containers.append(broker)
            _start_broker(broker, client_net, fixture_ip, tls, secret, fixture_image, broker_user)
            egress._docker("network", "connect", fixture_net, broker)
            egress._ready(broker)
            _check_mounts(
                broker,
                {
                    "/runner/codex_broker_child.py": (BROKER_CHILD, False),
                    "/runner/codex_broker_policy.py": (BROKER_POLICY, False),
                    "/tls": (tls, False),
                    "/broker-secret": (secret, False),
                },
                fixture_image,
                {client_net, fixture_net},
                broker_user,
            )
            containers.append(client)
            _start_client(
                client,
                client_net,
                image,
                egress._ip(broker, client_net),
                fixture_ip,
                repo,
                tls / "cert.pem",
                prompt,
                client_user,
            )
            _check_mounts(
                client,
                {
                    "/workspace": (repo, True),
                    "/etc/ssl/certs/ca-certificates.crt": (tls / "cert.pem", False),
                    "/prompt.txt": (prompt, False),
                },
                image,
                {client_net},
                client_user,
            )
            egress._docker("wait", client, timeout=35)
            state = egress._inspect("container", client)["State"]
            raw = egress._docker("logs", client, timeout=10).encode()
            if len(raw) > 65536 or state["ExitCode"] != 0:
                raise egress.ProofError("broker_client_terminal_unproved")
            events = [json.loads(line) for line in raw.splitlines()]
            boundaries = [item for item in events if item.get("type") == "boundary.proof"]
            if len(boundaries) != 1 or any(
                boundaries[0].get(key) is not True
                for key in (
                    "homeEmpty",
                    "fixtureDirectDenied",
                    "noDefaultRoute",
                    "noHostPaths",
                    "otherHostDenied",
                    "otherPortDenied",
                )
            ):
                raise egress.ProofError("broker_boundary_unproved")
            observations = [json.loads(line) for line in egress._docker("logs", fixture).splitlines()[1:]]
            broker_log = egress._docker("logs", broker).encode()
            fixture_log = egress._docker("logs", fixture).encode()
            broker_events = [json.loads(line) for line in broker_log.splitlines()[1:]]
            forwarded = [item for item in broker_events if item.get("type") == "broker.forwarded"]
            denied = [item for item in broker_events if item.get("type") == "broker.denied"]
            if (
                len(observations) != 2
                or not all(
                    item.get("authorized") is True and item.get("valid") is True for item in observations
                )
                or observations[0].get("prompt_bound") is not True
                or "exec_command" not in observations[0].get("tool_names", [])
                or "function_call_output" not in observations[1].get("input_types", [])
                or [item.get("sequence") for item in forwarded] != [1, 2]
                or [item.get("observed_tokens") for item in forwarded] != [14, 14]
                or len(denied) < 2
                or any(item.get("reason") != "connect_target_denied" for item in denied[:2])
            ):
                raise egress.ProofError("broker_request_evidence_unproved")
            completed, session, tokens, cost = _parse_events("codex_cli", raw)
            commands = [
                item["item"]
                for item in events
                if item.get("type") == "item.completed"
                and isinstance(item.get("item"), dict)
                and item["item"].get("type") == "command_execution"
            ]
            after, _ = _test(repo, Deadline(10), 16384, "after")
            if (
                not completed
                or not session
                or tokens != 28
                or cost is not None
                or len(commands) != 1
                or commands[0].get("exit_code") != 0
                or after["status"] != "passed"
                or (repo / "calculator.py").read_bytes()
                != b"def add_one(value: int) -> int:\n    return value + 1\n"
                or any(
                    sensitive in log
                    for log in (raw, broker_log, fixture_log)
                    for sensitive in (b"local-test-only", PROMPT.encode())
                )
            ):
                raise egress.ProofError("broker_repair_unproved")
            return {
                "image_id": image,
                "fixture_image_id": fixture_image,
                "prompt_sha256": sha256(PROMPT.encode()).hexdigest(),
                "provider_requests": len(forwarded),
                "denied_connects": len(denied),
                "reported_tokens": tokens,
                "cost_usd": None,
                "before": before,
                "after": after,
            }
    finally:
        local._cleanup(containers, networks, sys.exception())
