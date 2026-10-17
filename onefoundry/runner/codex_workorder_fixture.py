"""One synthetic Codex work-order invocation with a local-only Responses server.

This is an explicit test harness, not an installed runner capability. The only
repository mounted into Codex is the disposable engineer_qa_v1 checkout.
"""

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
from runner.codex_workorder_fixture_child import __file__ as CHILD_FILE
from runner.external_cli import CliRequest, CliResult, _parse_events
from runner.external_service import process_external_job
from runner.fixture_git import FIXTURE
from runner.isolated_fixture_test import run_isolated_fixture_test
from runner.provider_progress import LiveProgressParser, extract_progress

CHILD = Path(CHILD_FILE).resolve()
SHA_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_BOOT = """
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
  const privateHomeWasEmpty = !fs.existsSync(process.env.CODEX_HOME)
    && fs.readdirSync('/home/codex').length === 0;
  const fixtureDirectDenied = !(await canConnect(process.env.FIXTURE_IP, 443));
  const routes = fs.readFileSync('/proc/net/route', 'utf8').split('\\n').slice(1);
  const noDefaultRoute = routes.every(line => !line || line.trim().split(/\\s+/)[1] !== '00000000');
  const noHostPaths = ['/root/.codex', '/home/node/.codex', '/runner-spool',
    '/var/run/docker.sock', '/run/secrets', '/app'].every(path => !fs.existsSync(path));
  process.stdout.write(JSON.stringify({type:'boundary.proof', privateHomeWasEmpty,
    fixtureDirectDenied, noDefaultRoute, noHostPaths}) + '\\n');
  if (!privateHomeWasEmpty || !fixtureDirectDenied || !noDefaultRoute || !noHostPaths) process.exit(2);
  for (const entry of fs.readdirSync('/seed')) {
    fs.cpSync('/seed/' + entry, '/workspace/' + entry, {recursive:true});
  }
  fs.mkdirSync(process.env.CODEX_HOME, {recursive: true});
  const prompt = fs.readFileSync('/prompt.txt', 'utf8');
  const child = cp.spawn('/opt/codex-cli/node_modules/.bin/codex', process.argv.slice(1), {
    cwd: '/workspace', env: process.env, stdio: ['pipe', 'pipe', 'pipe']
  });
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
  const timer = setTimeout(() => {timedOut = true; child.kill('SIGKILL');}, 20000);
  const result = await new Promise(resolve => child.on('close', (code, signal) => resolve({code, signal})));
  clearTimeout(timer);
  if (spawnFailed || timedOut || outputExceeded || result.signal) {
    process.exitCode = 1;
    return;
  }
  const changed = fs.readFileSync('/workspace/calculator.py');
  if (changed.length > 4096) process.exit(2);
  process.stdout.write(JSON.stringify({type:'workspace.result',
    calculator_b64:changed.toString('base64')}) + '\\n');
  process.exitCode = result.code === null ? 1 : result.code;
})().catch(() => process.exit(2));
"""


def _start_fixture(
    name: str, network: str, tls_dir: Path, image_id: str, nonce: str, prompt_digest: str
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
        "--user=0:0",
        "--tmpfs",
        "/app:rw,noexec,nosuid,size=16m",
        "--mount",
        f"type=bind,src={CHILD},dst=/fixture.py,readonly",
        "--mount",
        f"type=bind,src={egress.CHILD},dst=/runner/egress_probe_child.py,readonly",
        "--mount",
        f"type=bind,src={proof.FIXTURE_CHILD},dst=/runner/codex_local_responses_child.py,readonly",
        "--mount",
        f"type=bind,src={tls_dir},dst=/tls,readonly",
        "--env",
        "PYTHONPATH=/",
        "--env",
        f"PROOF_NONCE={nonce}",
        "--env",
        f"PROMPT_SHA256={prompt_digest}",
        image_id,
        "/usr/local/bin/python3",
        "/fixture.py",
    )


def _start_client(
    name: str,
    image_id: str,
    network: str,
    proxy_ip: str,
    fixture_ip: str,
    repo: Path,
    cert: Path,
    prompt_path: Path,
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
        "--user=1000:1000",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=8m,nr_inodes=128,uid=1000,gid=1000",
        "--tmpfs",
        "/home/codex:rw,nosuid,nodev,size=32m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16m",  # noqa: S108 - private tmpfs
        "--mount",
        f"type=bind,src={repo},dst=/seed,readonly",
        "--mount",
        f"type=bind,src={cert},dst=/etc/ssl/certs/ca-certificates.crt,readonly",
        "--mount",
        f"type=bind,src={prompt_path},dst=/prompt.txt,readonly",
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
        NODE_BOOT,
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


def invoke_synthetic_codex(request: CliRequest, image_id: str, fixture_image: str) -> CliResult:
    """Run exactly one CLI turn; any ambiguous observation is unknown."""
    if request.adapter_id != "codex_cli" or not SHA_IMAGE.fullmatch(image_id):
        raise ValueError("synthetic_codex_image_unpinned")
    if not SHA_IMAGE.fullmatch(fixture_image):
        raise ValueError("synthetic_fixture_image_unpinned")
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
    egress._inspect("image", image_id)
    egress._inspect("image", fixture_image)
    suffix, nonce = uuid4().hex[:12], uuid4().hex
    client_net, fixture_net = f"ap-code-client-{suffix}", f"ap-code-fixture-{suffix}"
    fixture, proxy, client = f"ap-code-server-{suffix}", f"ap-code-proxy-{suffix}", f"ap-code-cli-{suffix}"
    networks: list[str] = []
    containers: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-code-tls-") as temporary:
            root = Path(temporary)
            root.chmod(0o711)
            tls_dir = root / "tls"
            tls_dir.mkdir(mode=0o755)
            prompt_path = root / "prompt.txt"
            prompt_path.write_bytes(request.prompt.encode())
            prompt_path.chmod(0o600)
            egress._certificate(tls_dir)
            proof._server_certificate(tls_dir)
            (tls_dir / "leaf-key.pem").chmod(0o644)
            for network in (client_net, fixture_net):
                networks.append(network)
                egress._network(network)
            containers.append(fixture)
            _start_fixture(
                fixture,
                fixture_net,
                tls_dir,
                fixture_image,
                nonce,
                sha256(request.prompt.encode()).hexdigest(),
            )
            try:
                egress._ready(fixture)
            except egress.ProofError as exc:
                raise egress.ProofError("synthetic_fixture_start_failed") from exc
            fixture_ip = egress._ip(fixture, fixture_net)
            containers.append(proxy)
            proof._start_proxy(proxy, client_net, fixture_ip, fixture_image)
            egress._docker("network", "connect", fixture_net, proxy)
            egress._ready(proxy)
            containers.append(client)
            _start_client(
                client,
                image_id,
                client_net,
                egress._ip(proxy, client_net),
                fixture_ip,
                request.repository,
                tls_dir / "cert.pem",
                prompt_path,
            )
            inspected = egress._inspect("container", client)
            host = inspected["HostConfig"]
            mounts = {item["Destination"]: item for item in inspected["Mounts"]}
            if (
                inspected["Image"] != image_id
                or set(inspected["NetworkSettings"]["Networks"]) != {client_net}
                or set(mounts) != {"/seed", "/etc/ssl/certs/ca-certificates.crt", "/prompt.txt"}
                or mounts["/seed"]["Source"] != str(request.repository.resolve())
                or mounts["/seed"]["RW"] is not False
                or host["Tmpfs"].get("/workspace")
                != "rw,nosuid,nodev,size=8m,nr_inodes=128,uid=1000,gid=1000"
                or mounts["/etc/ssl/certs/ca-certificates.crt"]["Source"] != str(tls_dir / "cert.pem")
                or mounts["/etc/ssl/certs/ca-certificates.crt"]["RW"] is not False
                or mounts["/prompt.txt"]["Source"] != str(prompt_path)
                or mounts["/prompt.txt"]["RW"] is not False
                or not host["ReadonlyRootfs"]
                or host["CapDrop"] != ["ALL"]
                or host["SecurityOpt"] != ["no-new-privileges:true"]
                or host["Memory"] != 512 * 1024 * 1024
                or host["MemorySwap"] != 512 * 1024 * 1024
                or host["PidsLimit"] != 64
                or host["Privileged"] is not False
                or host["NetworkMode"] != client_net
                or inspected["Config"]["User"] != "1000:1000"
            ):
                raise egress.ProofError("synthetic_codex_boundary_invalid")
            parser = LiveProgressParser("codex_cli") if request.on_progress is not None else None

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
            if (
                state["ExitCode"] != 0
                or len(observations) != 2
                or not all(
                    item.get("valid") is True and item.get("authorized") is True for item in observations
                )
                or "exec_command" not in observations[0]["tool_names"]
                or observations[0]["prompt_bound"] is not True
                or "function_call_output" not in observations[1]["input_types"]
                or sum(
                    item.get("connect") == "CONNECT provider.test:443 HTTP/1.1"
                    and item.get("allowed") is True
                    for item in proxy_events
                )
                != 2
                or sum(item.get("tls_sni") == "provider.test" for item in proxy_events) != 2
                or any(
                    item.get("allowed") is True
                    and item.get("connect") != "CONNECT provider.test:443 HTTP/1.1"
                    for item in proxy_events
                )
                or any("proxy_error" in item for item in proxy_events)
            ):
                raise egress.ProofError("synthetic_codex_turn_unproved")
            complete, session, tokens, cost = _parse_events("codex_cli", raw)
            events = [json.loads(line) for line in raw.splitlines()]
            boundary = [item for item in events if item.get("type") == "boundary.proof"]
            commands = [
                item["item"]
                for item in events
                if item.get("type") == "item.completed"
                and isinstance(item.get("item"), dict)
                and item["item"].get("type") == "command_execution"
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
                not complete
                or not session
                or tokens is None
                or len(boundary) != 1
                or any(
                    boundary[0].get(key) is not True
                    for key in ("privateHomeWasEmpty", "fixtureDirectDenied", "noDefaultRoute", "noHostPaths")
                )
                or len(commands) != 1
                or commands[0].get("status") != "completed"
                or commands[0].get("exit_code") != 0
                or changed != b"def add_one(value: int) -> int:\n    return value + 1\n"
            ):
                raise egress.ProofError("synthetic_codex_terminal_unproved")
            (request.repository / "calculator.py").write_bytes(changed)
            blocked = sum(item.get("allowed") is False for item in proxy_events)
            if blocked > 999:
                raise egress.ProofError("synthetic_proxy_observation_limit")
            progress = extract_progress("codex_cli", raw)
            return CliResult(
                "done",
                "provider_completed",
                session,
                tokens,
                cost,
                (
                    "synthetic_cli=codex_cli model=gpt-5 command=fixture_calculator_repair "
                    f"image_sha256={image_id} "
                    f"prompt_sha256={sha256(request.prompt.encode()).hexdigest()} "
                    f"allowed_provider_requests=2 blocked_other_connects={blocked}\n"
                ).encode(),
                progress.items,
                progress.omitted,
            )
    finally:
        proof._cleanup(containers, networks, sys.exception())


def process_synthetic_codex_job(
    path: Path, spool: Path, image_id: str, fixture_image: str
) -> dict[str, object] | None:
    """Test-only bridge for an API-dispatched manifest; no installed entrypoint."""

    def invoke(request: CliRequest) -> CliResult:
        try:
            return invoke_synthetic_codex(request, image_id, fixture_image)
        except (OSError, ValueError, egress.ProofError):
            return CliResult(
                "unknown",
                "synthetic_codex_launch_uncertain",
                None,
                None,
                None,
                b"synthetic_codex_fixture=unknown\n",
            )

    return process_external_job(
        path,
        spool,
        executable=Path("/opt/codex-cli/node_modules/.bin/codex"),
        invoke=invoke,
        test_runner=partial(run_isolated_fixture_test, image_id=fixture_image),
        launch_kind="synthetic_codex_fixture",
        image_id=image_id,
    )
