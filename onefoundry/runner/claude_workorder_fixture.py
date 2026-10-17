"""Test-only pinned Claude Code work-order turn against a local Messages fixture."""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
from functools import partial
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from runner import codex_local_responses_proof as proof
from runner import docker_egress_proof as egress
from runner.claude_workorder_fixture_child import __file__ as CHILD_FILE
from runner.external_cli import CliRequest, CliResult, _parse_events
from runner.external_service import process_external_job
from runner.fixture_git import FIXTURE
from runner.isolated_fixture_test import run_isolated_fixture_test
from runner.provider_progress import LiveProgressParser, extract_progress

CHILD = Path(CHILD_FILE).resolve()
IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_BOOT = r"""
const fs = require('fs');
const cp = require('child_process');
const net = require('net');
function canConnect(ip, port) {
  return new Promise(resolve => {
    const socket = net.createConnection({host: ip, port, timeout: 500});
    socket.once('connect', () => {socket.destroy(); resolve(true);});
    socket.once('error', () => {socket.destroy(); resolve(false);});
    socket.once('timeout', () => {socket.destroy(); resolve(false);});
  });
}
(async () => {
  const privateHomeWasEmpty = fs.readdirSync('/home/claude').length === 0;
  const fixtureDirectDenied = !(await canConnect(process.env.FIXTURE_IP, 443));
  const routes = fs.readFileSync('/proc/net/route', 'utf8').split('\n').slice(1);
  const noDefaultRoute = routes.every(line => !line || line.trim().split(/\s+/)[1] !== '00000000');
  const noHostPaths = ['/root/.claude', '/root/.codex', '/home/node/.claude',
    '/runner-spool', '/var/run/docker.sock', '/run/secrets', '/app'].every(path => !fs.existsSync(path));
  process.stdout.write(JSON.stringify({type:'boundary.proof', privateHomeWasEmpty,
    fixtureDirectDenied, noDefaultRoute, noHostPaths}) + '\n');
  if (!privateHomeWasEmpty || !fixtureDirectDenied || !noDefaultRoute || !noHostPaths) process.exit(2);
  for (const entry of fs.readdirSync('/seed')) {
    fs.cpSync('/seed/' + entry, '/workspace/' + entry, {recursive:true});
  }
  const prompt = fs.readFileSync('/prompt.txt', 'utf8');
  const child = cp.spawn('/opt/claude-code/node_modules/.bin/claude', [
    '--print', '--verbose', '--output-format', 'stream-json', '--input-format', 'text',
    '--bare', '--restricted', '--strict-mcp-config', '--tools', 'Read,Edit',
    '--no-session-persistence', '--max-turns', '3', '--permission-mode', 'acceptEdits',
    '--model', 'claude-sonnet-4-5'
  ], {cwd:'/workspace', env:process.env, stdio:['pipe', 'pipe', 'pipe']});
  let outputBytes = 0;
  let outputExceeded = false;
  let timedOut = false;
  let spawnFailed = false;
  function forward(source, target) {
    source.on('data', chunk => {
      if (outputExceeded) return;
      outputBytes += chunk.length;
      if (outputBytes > 65536) {
        outputExceeded = true;
        child.kill('SIGKILL');
        return;
      }
      if (!target.write(chunk)) {
        source.pause();
        target.once('drain', () => source.resume());
      }
    });
  }
  forward(child.stdout, process.stdout);
  forward(child.stderr, process.stderr);
  child.on('error', () => {spawnFailed = true;});
  child.stdin.on('error', () => {});
  child.stdin.end(prompt);
  const timer = setTimeout(() => {timedOut = true; child.kill('SIGKILL');}, 30000);
  const result = await new Promise(resolve => child.on('close', (code, signal) => resolve({code, signal})));
  clearTimeout(timer);
  if (spawnFailed || timedOut || outputExceeded || result.signal) {
    process.exitCode = 1;
    return;
  }
  const changed = fs.readFileSync('/workspace/calculator.py');
  if (changed.length > 4096) process.exit(2);
  process.stdout.write(JSON.stringify({type:'workspace.result',
    calculator_b64:changed.toString('base64')}) + '\n');
  process.exitCode = result.code === null ? 1 : result.code;
})().catch(() => process.exit(2));
"""


def _start_fixture(name: str, network: str, tls: Path, fixture_image: str, prompt_digest: str) -> None:
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
        f"type=bind,src={CHILD},dst=/fixture.py,readonly",
        "--mount",
        f"type=bind,src={proof.FIXTURE_CHILD},dst=/runner/codex_local_responses_child.py,readonly",
        "--mount",
        f"type=bind,src={egress.CHILD},dst=/runner/egress_probe_child.py,readonly",
        "--mount",
        f"type=bind,src={tls},dst=/tls,readonly",
        "--env",
        "PYTHONPATH=/",
        "--env",
        f"PROMPT_SHA256={prompt_digest}",
        fixture_image,
        "/usr/local/bin/python3",
        "/fixture.py",
    )


def _start_client(
    name: str,
    image: str,
    network: str,
    proxy_ip: str,
    fixture_ip: str,
    repo: Path,
    cert: Path,
    prompt: Path,
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
        "--memory=4g",
        "--memory-swap=4g",
        "--cpus=1",
        "--user=1000:1000",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=8m,nr_inodes=128,uid=1000,gid=1000",
        "--tmpfs",
        "/home/claude:rw,nosuid,nodev,size=128m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",  # noqa: S108 - private tmpfs
        "--mount",
        f"type=bind,src={repo},dst=/seed,readonly",
        "--mount",
        f"type=bind,src={cert},dst=/etc/ssl/certs/ca-certificates.crt,readonly",
        "--mount",
        f"type=bind,src={prompt},dst=/prompt.txt,readonly",
        "--env",
        "HOME=/home/claude",
        "--env",
        "ANTHROPIC_API_KEY=local-test-only",
        "--env",
        "ANTHROPIC_BASE_URL=https://provider.test",
        "--env",
        f"HTTPS_PROXY=http://{proxy_ip}:3128",
        "--env",
        f"HTTP_PROXY=http://{proxy_ip}:3128",
        "--env",
        "NO_PROXY=",
        "--env",
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt",
        "--env",
        "DISABLE_UPDATES=1",
        "--env",
        "DISABLE_TELEMETRY=1",
        "--env",
        "CLAUDE_CODE_MAX_RETRIES=0",
        "--env",
        "ENABLE_CLAUDEAI_MCP_SERVERS=false",
        "--env",
        f"FIXTURE_IP={fixture_ip}",
        "--entrypoint",
        "/usr/local/bin/node",
        image,
        "-e",
        NODE_BOOT,
    )


def invoke_synthetic_claude(request: CliRequest, image: str, fixture_image: str) -> CliResult:
    """Run one bounded real Claude CLI turn; uncertain observations fail closed."""
    if (
        request.adapter_id != "claude_code"
        or not IMAGE.fullmatch(image)
        or not IMAGE.fullmatch(fixture_image)
    ):
        raise ValueError("synthetic_claude_image_unpinned")
    if request.repository.is_symlink() or not request.repository.is_dir():
        raise ValueError("synthetic_repository_invalid")
    if not request.prompt or len(request.prompt.encode()) > 8192:
        raise ValueError("synthetic_prompt_invalid")
    if any(
        (request.repository / relative).is_symlink()
        or (request.repository / relative).read_bytes() != (FIXTURE / relative).read_bytes()
        for relative in ("calculator.py", "tests/test_calculator.py")
    ):
        raise ValueError("synthetic_repository_not_fixture")
    egress._inspect("image", image)
    egress._inspect("image", fixture_image)
    suffix = uuid4().hex[:12]
    client_net, fixture_net = f"ap-claude-client-{suffix}", f"ap-claude-fixture-{suffix}"
    fixture, proxy, client = (
        f"ap-claude-server-{suffix}",
        f"ap-claude-proxy-{suffix}",
        f"ap-claude-cli-{suffix}",
    )
    networks: list[str] = []
    containers: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-claude-tls-") as temporary:
            root = Path(temporary)
            root.chmod(0o711)
            tls = root / "tls"
            tls.mkdir(mode=0o755)
            prompt = root / "prompt.txt"
            prompt.write_bytes(request.prompt.encode())
            prompt.chmod(0o600)
            egress._certificate(tls)
            proof._server_certificate(tls)
            (tls / "leaf-key.pem").chmod(0o644)
            for network in (client_net, fixture_net):
                networks.append(network)
                egress._network(network)
            containers.append(fixture)
            _start_fixture(
                fixture, fixture_net, tls, fixture_image, sha256(request.prompt.encode()).hexdigest()
            )
            egress._ready(fixture)
            fixture_ip = egress._ip(fixture, fixture_net)
            containers.append(proxy)
            proof._start_proxy(proxy, client_net, fixture_ip, fixture_image)
            egress._docker("network", "connect", fixture_net, proxy)
            egress._ready(proxy)
            containers.append(client)
            _start_client(
                client,
                image,
                client_net,
                egress._ip(proxy, client_net),
                fixture_ip,
                request.repository,
                tls / "cert.pem",
                prompt,
            )
            inspected = egress._inspect("container", client)
            host = inspected["HostConfig"]
            mounts = {item["Destination"]: item for item in inspected["Mounts"]}
            if (
                inspected["Image"] != image
                or set(inspected["NetworkSettings"]["Networks"]) != {client_net}
                or set(mounts) != {"/seed", "/etc/ssl/certs/ca-certificates.crt", "/prompt.txt"}
                or mounts["/seed"]["Source"] != str(request.repository.resolve())
                or mounts["/seed"]["RW"] is not False
                or host["Tmpfs"].get("/workspace")
                != "rw,nosuid,nodev,size=8m,nr_inodes=128,uid=1000,gid=1000"
                or mounts["/prompt.txt"]["RW"] is not False
                or not host["ReadonlyRootfs"]
                or host["CapDrop"] != ["ALL"]
                or host["SecurityOpt"] != ["no-new-privileges:true"]
                or host["Memory"] != 4 * 1024**3
                or host["MemorySwap"] != 4 * 1024**3
                or host["PidsLimit"] != 64
                or host["Privileged"] is not False
                or host["NetworkMode"] != client_net
                or inspected["Config"]["User"] != "1000:1000"
            ):
                raise egress.ProofError("synthetic_claude_boundary_invalid")
            parser = LiveProgressParser("claude_code") if request.on_progress is not None else None

            def publish(chunk: bytes) -> None:
                if parser is not None and request.on_progress is not None:
                    for update in parser.feed(chunk):
                        request.on_progress(update)

            raw = egress.bounded_logs(
                client,
                request.max_output_bytes,
                timeout=request.wall_seconds,
                follow=True,
                on_stdout_chunk=publish if parser is not None else None,
            )
            state = egress._inspect("container", client)["State"]
            observations = [json.loads(line) for line in egress.bounded_logs(fixture, 65536).splitlines()[1:]]
            proxy_events = [json.loads(line) for line in egress.bounded_logs(proxy, 65536).splitlines()[1:]]
            stages = [item["stage"] for item in observations if "stage" in item and item["stage"] != "title"]
            if (
                state["ExitCode"] != 0
                or stages != [1, 2, 3]
                or not any(
                    item.get("stage") == 1 and item.get("prompt_bound") is True for item in observations
                )
                or len(observations) > 5
                or any("fixture_error" in item for item in observations)
                or sum(
                    item.get("connect") == "CONNECT provider.test:443 HTTP/1.1"
                    and item.get("allowed") is True
                    for item in proxy_events
                )
                != len(observations)
                or sum(item.get("tls_sni") == "provider.test" for item in proxy_events) != len(observations)
                or any(
                    item.get("allowed") is True
                    and item.get("connect") != "CONNECT provider.test:443 HTTP/1.1"
                    for item in proxy_events
                )
                or any("proxy_error" in item for item in proxy_events)
            ):
                raise egress.ProofError("synthetic_claude_turn_unproved")
            events = [json.loads(line) for line in raw.splitlines()]
            boundary = [item for item in events if item.get("type") == "boundary.proof"]
            completed, session, tokens, _cost = _parse_events("claude_code", raw)
            result = [item for item in events if item.get("type") == "result"]
            tool_results = [
                item
                for item in events
                if item.get("type") == "user" and isinstance(item.get("message"), dict)
            ]
            workspace = [item for item in events if item.get("type") == "workspace.result"]
            try:
                changed = (
                    base64.b64decode(workspace[0]["calculator_b64"], validate=True)
                    if len(workspace) == 1 and isinstance(workspace[0].get("calculator_b64"), str)
                    else None
                )
            except (ValueError, KeyError):
                changed = None
            if (
                not completed
                or not session
                or tokens is None
                or len(result) != 1
                or result[0].get("permission_denials") != []
                or len(boundary) != 1
                or any(
                    boundary[0].get(key) is not True
                    for key in ("privateHomeWasEmpty", "fixtureDirectDenied", "noDefaultRoute", "noHostPaths")
                )
                or len(tool_results) != 2
                or changed != b"def add_one(value: int) -> int:\n    return value + 1\n"
            ):
                raise egress.ProofError("synthetic_claude_terminal_unproved")
            (request.repository / "calculator.py").write_bytes(changed)
            blocked = sum(item.get("allowed") is False for item in proxy_events)
            if blocked > 999:
                raise egress.ProofError("synthetic_proxy_observation_limit")
            progress = extract_progress("claude_code", raw)
            return CliResult(
                "done",
                "provider_completed",
                session,
                tokens,
                None,
                (
                    "synthetic_cli=claude_code model=claude-sonnet-4-5 command=fixture_calculator_repair "
                    f"image_sha256={image} prompt_sha256={sha256(request.prompt.encode()).hexdigest()} "
                    f"allowed_provider_requests={len(observations)} blocked_other_connects={blocked}\n"
                ).encode(),
                progress.items,
                progress.omitted,
            )
    finally:
        proof._cleanup(containers, networks, sys.exception())


def process_synthetic_claude_job(
    path: Path, spool: Path, image: str, fixture_image: str
) -> dict[str, object] | None:
    """Test-only API-manifest bridge; installed runner remains disabled."""

    def invoke(request: CliRequest) -> CliResult:
        try:
            return invoke_synthetic_claude(request, image, fixture_image)
        except (OSError, ValueError, egress.ProofError):
            return CliResult("unknown", "synthetic_claude_launch_uncertain", None, None, None, b"")

    return process_external_job(
        path,
        spool,
        executable=Path("/opt/claude-code/node_modules/.bin/claude"),
        invoke=invoke,
        test_runner=partial(run_isolated_fixture_test, image_id=fixture_image),
        launch_kind="synthetic_claude_fixture",
        image_id=image,
    )
