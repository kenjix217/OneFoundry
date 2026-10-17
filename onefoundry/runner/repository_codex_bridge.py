"""Disposable Codex CLI behind an exact credential broker and verified OS boundary."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from onefoundry.repository_code_contract import RepositoryCodeManifest, digest
from runner import docker_egress_proof as docker
from runner.external_cli import CliResult
from runner.fixture_git import Deadline, ExecutionError
from runner.isolated_fixture_test import _bounded_attach
from runner.repository_candidate_observation import copy_failure
from runner.repository_codex_observation import CLI_OBSERVER
from runner.repository_codex_observation import result as observe_result
from runner.repository_provider_diagnostics import summarize
from runner.repository_runner_credentials import broker_credential
from runner.repository_runner_settings import RepositoryRunnerSettings
from runner.repository_source_view import copy_changes, file_inventory, make_view, validate_edit_scope

BROKER = Path(__file__).with_name("repository_provider_broker.py").resolve()
POLICY = Path(__file__).with_name("repository_provider_policy.py").resolve()

CLIENT_BOOT = (
    CLI_OBSERVER
    + r"""
const fs = require('fs'), cp = require('child_process'), net = require('net'), http = require('http');
function connect(host, port) { return new Promise(resolve => {
  const socket = net.createConnection({host, port, timeout:500});
  socket.once('connect', () => {socket.destroy(); resolve(true);});
  socket.once('error', () => resolve(false));
  socket.once('timeout', () => {socket.destroy(); resolve(false);});
}); }
function deniedRoute(path) { return new Promise(resolve => {
  const req = http.request({host:process.env.BROKER_IP, port:Number(process.env.BROKER_PORT), path,
    method:'POST', timeout:1000,
    headers:{Authorization:'Bearer client-placeholder-only','Content-Length':'0'}},
    res => {res.resume(); resolve(res.statusCode !== 200);});
  req.once('error', () => resolve(false));
  req.once('timeout', () => {req.destroy(); resolve(false);}); req.end();
}); }
(async () => {
  const route = fs.readFileSync('/proc/net/route','utf8').split('\n').slice(1);
  const noDefaultRoute = route.every(line => !line.trim() || line.trim().split(/\s+/)[1] !== '00000000');
  const noHostPaths = ['/workspace/.git','/runner-spool','/source-roots','/provider-credential',
    '/var/run/docker.sock','/run/secrets','/app'].every(path => !fs.existsSync(path));
  const privateHome = !fs.existsSync(process.env.CODEX_HOME) && fs.readdirSync(process.env.HOME).length === 0;
  const directDenied = !(await connect('1.1.1.1',443));
  const providerDirectDenied = !(await connect(process.env.PROVIDER_HOST,443));
  const otherRouteDenied = await deniedRoute('/not-the-approved-provider');
  const otherPortDenied = !(await connect(process.env.BROKER_IP, Number(process.env.BROKER_PORT)+1));
  const proof = {type:'repository.boundary', noDefaultRoute,noHostPaths,privateHome,directDenied,
    providerDirectDenied,otherRouteDenied,otherPortDenied};
  process.stdout.write(JSON.stringify(proof)+'\n');
  if (!Object.entries(proof).filter(([key]) => key !== 'type')
    .every(([,value]) => value === true)) process.exit(2);
  if (process.env.PROBE_ONLY === '1') process.exit(0);
  fs.mkdirSync(process.env.CODEX_HOME, {recursive:true});
  await observeCli(process.env.CLI_EXECUTABLE, process.argv.slice(1), {
    cwd:'/workspace', env:process.env, input:fs.readFileSync('/prompt.txt','utf8'),
    wallMilliseconds:Number(process.env.WALL_MILLISECONDS), recordLimit:Number(process.env.OUTPUT_BYTES)
  });
})().catch(() => process.exit(2));
"""
)


def _attach_client(name: str, deadline: Deadline, output_limit: int) -> bytes:
    try:
        return _bounded_attach(name, deadline, output_limit)
    except TimeoutError as exc:
        raise ExecutionError("maintenance_cli_timeout") from exc


@dataclass(frozen=True)
class RepositoryCodexBridge:
    settings: RepositoryRunnerSettings
    fixture_network: str | None = None
    fixture_ip: str | None = None
    fixture_tls: Path | None = None

    def _boundary(
        self,
        repo: Path,
        prompt: str,
        deadline: Deadline,
        *,
        probe_only: bool,
        output_limit: int,
        reasoning_effort: str | None = None,
    ) -> tuple[bytes, dict[str, bool]]:
        config = self.settings
        config.validate_host()
        token = uuid4().hex[:12]
        client_net, public_net = f"onefoundry-code-{token}", f"onefoundry-code-provider-{token}"
        broker, client = f"onefoundry-code-broker-{token}", f"onefoundry-code-client-{token}"
        networks: list[str] = []
        containers: list[str] = []
        from urllib.parse import urlsplit

        host = str(urlsplit(config.provider_url).hostname)
        with tempfile.TemporaryDirectory(prefix="onefoundry-provider-broker-") as temporary:
            work = Path(temporary)
            prompt_file, config_file = work / "prompt.txt", work / "broker.json"
            credential_file = work / "access-token"
            access_token, account_id = broker_credential(config)
            credential_file.write_bytes(access_token)
            credential_file.chmod(0o600)
            prompt_file.write_text(prompt)
            prompt_file.chmod(0o600)
            try:
                networks.append(client_net)
                docker._network(client_net)
                subnet = docker._inspect("network", client_net)["IPAM"]["Config"][0]["Subnet"]
                client_ip = str(ipaddress.ip_network(subnet).network_address + 3)
                if self.fixture_network is None:
                    networks.append(public_net)
                    docker._docker("network", "create", public_net)
                provider_net = self.fixture_network or public_net
                policy = config.policy
                value: dict[str, Any] = {
                    "model_id": config.model_id,
                    "upstream_url": config.provider_url,
                    "broker_port": config.broker_port,
                    "client_ip": client_ip,
                    "max_requests": policy.max_provider_requests,
                    "max_request_bytes": policy.max_provider_request_bytes,
                    "max_response_bytes": policy.max_provider_response_bytes,
                    "max_output_tokens": policy.max_provider_output_tokens,
                    "request_timeout_seconds": config.request_timeout_seconds,
                    "provider_overload_retries": config.provider_overload_retries,
                    "provider_overload_retry_delay_seconds": config.provider_overload_retry_delay_seconds,
                    "auth_kind": config.auth_kind,
                    "account_id": account_id,
                }
                extra: tuple[str, ...] = ()
                if self.fixture_network is not None:
                    if host != "provider.test" or self.fixture_ip is None or self.fixture_tls is None:
                        raise ValueError("repository_fixture_configuration_invalid")
                    value.update(fixture_upstream_ip=self.fixture_ip, fixture_ca_file="/fixture-tls/cert.pem")
                    extra = ("--mount", f"type=bind,src={self.fixture_tls},dst=/fixture-tls,readonly")
                config_file.write_text(json.dumps(value))
                config_file.chmod(0o600)
                user = f"{os.getuid()}:{os.getgid()}"
                resources = (
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    f"--pids-limit={config.child_processes}",
                    f"--memory={config.child_memory_mb}m",
                    f"--memory-swap={config.child_memory_mb}m",
                    f"--cpus={config.child_cpus}",
                    "--user",
                    user,
                    "--log-driver=none",
                )
                containers.append(broker)
                docker._docker(
                    "run",
                    "--detach",
                    "--pull=never",
                    "--name",
                    broker,
                    "--network",
                    provider_net,
                    *resources[:-1],
                    "--log-driver=local",
                    "--log-opt=max-size=64k",
                    "--log-opt=max-file=1",
                    "--log-opt=compress=false",
                    "--mount",
                    f"type=bind,src={BROKER},dst=/runner/repository_provider_broker.py,readonly",
                    "--mount",
                    f"type=bind,src={POLICY},dst=/runner/repository_provider_policy.py,readonly",
                    "--mount",
                    f"type=bind,src={config_file},dst=/broker.json,readonly",
                    "--mount",
                    f"type=bind,src={credential_file},dst=/provider-credential,readonly",
                    *extra,
                    "--env",
                    "PYTHONPATH=/",
                    f"--entrypoint={config.broker_executable}",
                    config.broker_image,
                    "/runner/repository_provider_broker.py",
                    "/broker.json",
                    timeout=min(config.request_timeout_seconds, max(1, int(deadline.remaining()))),
                )
                docker._docker("network", "connect", client_net, broker)
                broker_ip = docker._ip(broker, client_net)
                provider = {
                    "name": "OneFoundry bounded broker",
                    "base_url": f"http://{broker_ip}:{config.broker_port}/v1",
                    "env_key": "OPENAI_API_KEY",
                    "wire_api": "responses",
                    "supports_websockets": False,
                    "requires_openai_auth": False,
                    "request_max_retries": 0,
                    "stream_max_retries": 0,
                }
                # JSON is also valid TOML for these scalar table values when the
                # keys and booleans are rendered explicitly below.
                provider_toml = (
                    "{" + ",".join(f"{key}={json.dumps(val)}" for key, val in provider.items()) + "}"
                )
                argv = (
                    "--ask-for-approval",
                    "never",
                    "-c",
                    'model_provider="onefoundry_broker"',
                    "-c",
                    "model_providers.onefoundry_broker=" + provider_toml,
                    "-c",
                    'web_search="disabled"',
                    "-m",
                    config.model_id,
                    *(
                        ("-c", "model_reasoning_effort=" + json.dumps(reasoning_effort))
                        if reasoning_effort is not None
                        else ()
                    ),
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
                env = {
                    "HOME": "/private-home",
                    "CODEX_HOME": "/private-home/codex",
                    "BROKER_IP": broker_ip,
                    "BROKER_PORT": str(config.broker_port),
                    "PROVIDER_HOST": host,
                    "OPENAI_API_KEY": "client-placeholder-only",
                    "CLI_EXECUTABLE": config.cli_executable,
                    "WALL_MILLISECONDS": str(int(deadline.remaining() * 1000)),
                    "OUTPUT_BYTES": str(output_limit),
                    "PROBE_ONLY": "1" if probe_only else "0",
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                    "NO_PROXY": "*",
                }
                env_args = tuple(part for key, val in env.items() for part in ("--env", f"{key}={val}"))
                containers.append(client)
                docker._docker(
                    "create",
                    "--pull=never",
                    "--name",
                    client,
                    "--network",
                    client_net,
                    "--ip",
                    client_ip,
                    "--dns=127.0.0.1",
                    *resources,
                    "--tmpfs",
                    f"/private-home:rw,nosuid,nodev,size={config.private_tmp_mb}m,"
                    f"uid={os.getuid()},gid={os.getgid()},mode=0700",
                    "--tmpfs",
                    f"/tmp:rw,nosuid,nodev,size={config.private_tmp_mb}m",  # noqa: S108 - private container tmpfs
                    "--mount",
                    f"type=bind,src={repo},dst=/workspace",
                    "--mount",
                    f"type=bind,src={prompt_file},dst=/prompt.txt,readonly",
                    "--workdir=/workspace",
                    *env_args,
                    "--entrypoint=/usr/local/bin/node",
                    config.cli_image,
                    "-e",
                    CLIENT_BOOT,
                    "--",
                    *argv,
                    timeout=min(config.request_timeout_seconds, max(1, int(deadline.remaining()))),
                )
                self._inspect(
                    client,
                    config.cli_image,
                    {client_net},
                    {"/workspace": (repo, True), "/prompt.txt": (prompt_file, False)},
                )
                expected = {
                    "/runner/repository_provider_broker.py": (BROKER, False),
                    "/runner/repository_provider_policy.py": (POLICY, False),
                    "/broker.json": (config_file, False),
                    "/provider-credential": (credential_file, False),
                }
                if self.fixture_tls is not None:
                    expected["/fixture-tls"] = (self.fixture_tls, False)
                self._inspect(broker, config.broker_image, {client_net, provider_net}, expected)
                raw = _attach_client(client, deadline, output_limit)
                state = docker._inspect("container", client)["State"]
                records = [
                    json.loads(line)
                    for line in raw.splitlines()
                    if line.startswith(b'{"type":"repository.boundary"')
                ]
                if (
                    len(records) != 1
                    or state["ExitCode"] != 0
                    or any(val is not True for key, val in records[0].items() if key != "type")
                ):
                    observed = docker.bounded_logs(broker, 65536)
                    if self.fixture_network is not None:
                        raise ValueError("repository_fixture_cli_failed:" + observed.decode())
                    raise ValueError("repository_coding_boundary_or_cli_failed")
                diagnostics = summarize(docker.bounded_logs(broker, 65536))
                raw += b"\n" + json.dumps(diagnostics, sort_keys=True).encode() + b"\n"
                return raw, {key: val for key, val in records[0].items() if key != "type"}
            finally:
                for name in reversed(containers):
                    docker._remove("container", name)
                for name in reversed(networks):
                    docker._remove("network", name)

    def _inspect(
        self, name: str, image: str, networks: set[str], mounts: dict[str, tuple[Path, bool]]
    ) -> None:
        state = docker._inspect("container", name)
        host = state["HostConfig"]
        config = self.settings
        if (
            state["Image"] != image
            or set(state["NetworkSettings"]["Networks"]) != networks
            or host["Privileged"] is not False
            or host["ReadonlyRootfs"] is not True
            or host["CapDrop"] != ["ALL"]
            or host["SecurityOpt"] != ["no-new-privileges:true"]
            or host["Memory"] != config.child_memory_mb * 1024 * 1024
            or host["MemorySwap"] != host["Memory"]
            or host["PidsLimit"] != config.child_processes
            or host["NanoCpus"] != config.child_cpus * 1000000000
            or state["Config"]["User"] != f"{os.getuid()}:{os.getgid()}"
            or {item["Destination"] for item in state["Mounts"]} != set(mounts)
            or any(
                item["Source"] != str(mounts[item["Destination"]][0])
                or item["RW"] is not mounts[item["Destination"]][1]
                for item in state["Mounts"]
            )
        ):
            raise ValueError("repository_coding_topology_invalid")

    def prove(self) -> str:
        with tempfile.TemporaryDirectory(prefix="onefoundry-coding-admission-") as temporary:
            repo = Path(temporary) / "view"
            repo.mkdir()
            _, proof = self._boundary(
                repo,
                "Admission only.",
                Deadline(self.settings.request_timeout_seconds),
                probe_only=True,
                output_limit=8192,
            )
        return digest(
            {"image": self.settings.cli_image, "provider": self.settings.provider_url, "proof": proof}
        )

    def __call__(self, job: RepositoryCodeManifest, repo: Path, deadline: Deadline) -> CliResult:
        self.validate_job(job)
        with tempfile.TemporaryDirectory(prefix="onefoundry-coding-view-") as temporary:
            return self._code(job, repo, Path(temporary), deadline)

    def validate_job(self, job: RepositoryCodeManifest) -> None:
        config = self.settings
        if (
            job.adapter_id != "codex_cli"
            or job.model_id != config.model_id
            or job.target.provider_url != config.provider_url
            or job.stage_limits != config.policy
        ):
            raise ValueError("repository_runner_differs_from_approved_pilot")

    def validate_context(self, job: RepositoryCodeManifest, repo: Path) -> None:
        self.validate_job(job)
        validate_edit_scope(job, repo)

    def _code(
        self, job: RepositoryCodeManifest, repo: Path, temporary: Path, deadline: Deadline
    ) -> CliResult:
        plain = temporary / "view"
        make_view(repo, plain, job.target.allowed_paths, job.stage_limits.max_context_file_bytes)
        before = file_inventory(plain)
        prompt = (
            job.task_title
            + "\n"
            + job.task_instructions
            + "\nEdit only these approved paths:\n"
            + "\n".join(job.target.allowed_paths)
        )
        raw, _ = self._boundary(
            plain,
            prompt,
            deadline,
            probe_only=False,
            output_limit=job.limits["max_log_bytes"],
            reasoning_effort=job.target.reasoning_effort,
        )
        observed = observe_result(raw)
        if observed.status != "done":
            return observed
        try:
            copy_changes(job, plain, repo, before)
        except (OSError, ValueError) as exc:
            raise copy_failure(exc, observed) from exc
        return observed
