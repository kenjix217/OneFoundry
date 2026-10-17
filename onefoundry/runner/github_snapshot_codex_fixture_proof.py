"""Standalone fake HTTPS snapshot to pinned Codex CLI proof; no app dispatch."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from runner import codex_broker_proof as broker_proof
from runner import codex_local_responses_proof as local_proof
from runner import docker_egress_proof as egress
from runner import github_https_fixture_server as https_fixture
from runner.external_cli import CliRequest, CliResult, _parse_events
from runner.fixture_git import Deadline, _git, _run
from runner.github_fixture_snapshot_materialize import materialize_snapshot

CHILD = Path(__file__).with_name("github_snapshot_codex_fixture_child.py").resolve()
BEFORE: bytes = https_fixture.DATA
AFTER = b"synthetic HTTPS fixture repaired\n"
PROMPT = "Edit only file.txt so its exact contents become: synthetic HTTPS fixture repaired\\n"


class SnapshotCodexProofError(ValueError):
    """A fake-only snapshot coding proof failed closed."""


@dataclass(frozen=True)
class SnapshotCodexProof:
    source_tree: str
    snapshot_sha256: str
    synthetic_baseline_commit: str
    before_text_check: str
    after_text_check: str
    diff_sha256: str
    provider_session_id: str
    tokens_used: int
    cost_usd: float | None
    sanitized_log: bytes


def _start_server(name: str, network: str, tls: Path, image_id: str, prompt_digest: str) -> None:
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
        f"type=bind,src={local_proof.FIXTURE_CHILD},dst=/runner/codex_local_responses_child.py,readonly",
        "--mount",
        f"type=bind,src={tls},dst=/tls,readonly",
        "--env",
        "PYTHONPATH=/",
        "--env",
        f"PROMPT_SHA256={prompt_digest}",
        image_id,
        "/usr/local/bin/python3",
        "/fixture.py",
    )


def invoke_file_codex(
    request: CliRequest, image_id: str, fixture_image_id: str, attempt_id: str | None = None
) -> CliResult:
    """Use the credential broker and isolated Codex client with a fixed file edit server."""
    if (
        request.adapter_id != "codex_cli"
        or broker_proof.PINNED_IMAGE.fullmatch(image_id) is None
        or broker_proof.PINNED_IMAGE.fullmatch(fixture_image_id) is None
        or request.repository.is_symlink()
        or not request.repository.is_dir()
        or not request.prompt
        or len(request.prompt.encode()) > 8192
    ):
        raise SnapshotCodexProofError("fake_codex_request_invalid")
    egress._inspect("image", image_id)
    egress._inspect("image", fixture_image_id)
    broker_user, client_user = broker_proof._proof_users()
    suffix = uuid4().hex[:12] if attempt_id is None else str(UUID(attempt_id)).replace("-", "")
    client_net, fixture_net = f"ap-snapshot-client-{suffix}", f"ap-snapshot-fixture-{suffix}"
    fixture, broker, client = (
        f"ap-snapshot-server-{suffix}",
        f"ap-snapshot-broker-{suffix}",
        f"ap-snapshot-cli-{suffix}",
    )
    networks: list[str] = []
    containers: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-snapshot-codex-tls-") as temporary:
            root = Path(temporary)
            root.chmod(0o711)
            tls = root / "tls"
            tls.mkdir(mode=0o755)
            prompt = root / "prompt.txt"
            prompt.write_bytes(request.prompt.encode())
            prompt.chmod(0o600)
            broker_proof._share_disposable_client_mounts(
                request.repository, request.repository / "file.txt", prompt
            )
            secret = root / "broker-secret"
            secret.write_bytes(b"local-test-only")
            secret.chmod(0o600)
            egress._certificate(tls)
            local_proof._server_certificate(tls)
            (tls / "leaf-key.pem").chmod(0o644)
            for network in (client_net, fixture_net):
                networks.append(network)
                egress._network(network)
            containers.append(fixture)
            _start_server(
                fixture,
                fixture_net,
                tls,
                fixture_image_id,
                hashlib.sha256(request.prompt.encode()).hexdigest(),
            )
            egress._ready(fixture)
            fixture_ip = egress._ip(fixture, fixture_net)
            containers.append(broker)
            broker_proof._start_broker(
                broker, client_net, fixture_ip, tls, secret, fixture_image_id, broker_user
            )
            egress._docker("network", "connect", fixture_net, broker)
            egress._ready(broker)
            broker_proof._check_mounts(
                broker,
                {
                    "/runner/codex_broker_child.py": (broker_proof.BROKER_CHILD, False),
                    "/runner/codex_broker_policy.py": (broker_proof.BROKER_POLICY, False),
                    "/tls": (tls, False),
                    "/broker-secret": (secret, False),
                },
                fixture_image_id,
                {client_net, fixture_net},
                broker_user,
            )
            containers.append(client)
            broker_proof._start_client(
                client,
                client_net,
                image_id,
                egress._ip(broker, client_net),
                fixture_ip,
                request.repository,
                tls / "cert.pem",
                prompt,
                client_user,
            )
            broker_proof._check_mounts(
                client,
                {
                    "/workspace": (request.repository, True),
                    "/etc/ssl/certs/ca-certificates.crt": (tls / "cert.pem", False),
                    "/prompt.txt": (prompt, False),
                },
                image_id,
                {client_net},
                client_user,
            )
            inspected = egress._inspect("container", client)
            host = inspected["HostConfig"]
            mounts = {item["Destination"]: item for item in inspected["Mounts"]}
            if (
                inspected["Image"] != image_id
                or set(inspected["NetworkSettings"]["Networks"]) != {client_net}
                or set(mounts) != {"/workspace", "/etc/ssl/certs/ca-certificates.crt", "/prompt.txt"}
                or mounts["/workspace"]["Source"] != str(request.repository.resolve())
                or mounts["/workspace"]["RW"] is not True
                or mounts["/prompt.txt"]["Source"] != str(prompt)
                or mounts["/prompt.txt"]["RW"] is not False
                or not host["ReadonlyRootfs"]
                or host["CapDrop"] != ["ALL"]
                or host["SecurityOpt"] != ["no-new-privileges:true"]
                or host["Memory"] != 512 * 1024 * 1024
                or host["MemorySwap"] != 512 * 1024 * 1024
                or host["PidsLimit"] != 64
                or host["Privileged"] is not False
                or host["NetworkMode"] != client_net
                or inspected["Config"]["User"] != client_user
                or "OPENAI_API_KEY=client-placeholder-only" not in inspected["Config"]["Env"]
                or any("local-test-only" in value for value in inspected["Config"]["Env"])
            ):
                raise SnapshotCodexProofError("fake_codex_boundary_invalid")
            egress._docker("wait", client, timeout=request.wall_seconds)
            state = egress._inspect("container", client)["State"]
            raw = egress._docker("logs", client, timeout=10).encode()
            if len(raw) > request.max_output_bytes:
                raise SnapshotCodexProofError("fake_codex_output_limit")
            fixture_log = egress._docker("logs", fixture).encode()
            broker_log = egress._docker("logs", broker).encode()
            observations = [json.loads(line) for line in fixture_log.splitlines()[1:]]
            broker_events = [json.loads(line) for line in broker_log.splitlines()[1:]]
            forwarded = [item for item in broker_events if item.get("type") == "broker.forwarded"]
            denied = [item for item in broker_events if item.get("type") == "broker.denied"]
            if (
                state["ExitCode"] != 0
                or len(observations) != 2
                or any(
                    item.get("valid") is not True or item.get("authorized") is not True
                    for item in observations
                )
                or observations[0].get("prompt_bound") is not True
                or "exec_command" not in observations[0].get("tool_names", [])
                or observations[1].get("completed") is not True
                or [item.get("sequence") for item in forwarded] != [1, 2]
                or [item.get("observed_tokens") for item in forwarded] != [14, 14]
                or len(denied) < 2
                or any(item.get("reason") != "connect_target_denied" for item in denied[:2])
            ):
                raise SnapshotCodexProofError("fake_codex_turn_unproved")
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
            if (
                not complete
                or not session
                or tokens is None
                or cost is not None
                or len(boundary) != 1
                or any(
                    boundary[0].get(key) is not True
                    for key in (
                        "homeEmpty",
                        "fixtureDirectDenied",
                        "noDefaultRoute",
                        "noHostPaths",
                        "otherHostDenied",
                        "otherPortDenied",
                    )
                )
                or len(commands) != 1
                or commands[0].get("status") != "completed"
                or commands[0].get("exit_code") != 0
                or (request.repository / "file.txt").read_bytes() != AFTER
                or any(
                    sensitive in log
                    for log in (raw, broker_log, fixture_log)
                    for sensitive in (b"local-test-only", request.prompt.encode())
                )
            ):
                raise SnapshotCodexProofError("fake_codex_terminal_unproved")
            blocked = len(denied)
            if blocked > 999:
                raise SnapshotCodexProofError("fake_codex_proxy_limit")
            prompt_digest = hashlib.sha256(request.prompt.encode()).hexdigest()
            return CliResult(
                "done",
                "provider_completed",
                session,
                tokens,
                cost,
                (
                    f"synthetic_cli=codex_cli model=gpt-5 command=snapshot_file_repair "
                    f"image_sha256={image_id} prompt_sha256={prompt_digest} "
                    f"allowed_provider_requests=2 blocked_other_connects={blocked}\n"
                ).encode(),
            )
    finally:
        local_proof._cleanup(containers, networks, sys.exception())


def prove_snapshot_codex_fixture(
    snapshot_root: Path,
    snapshot_relative_path: str,
    snapshot_sha256: str,
    snapshot_byte_size: int,
    expected_tree: str,
    work_root: Path,
    image_id: str,
    fixture_image_id: str,
) -> SnapshotCodexProof:
    """Prove one exact file edit; the snapshot root never enters the client."""
    if (
        broker_proof.PINNED_IMAGE.fullmatch(image_id) is None
        or broker_proof.PINNED_IMAGE.fullmatch(fixture_image_id) is None
    ):
        raise SnapshotCodexProofError("fake_codex_image_unpinned")
    deadline = Deadline(60)
    with materialize_snapshot(
        snapshot_root, snapshot_relative_path, snapshot_sha256, snapshot_byte_size, work_root
    ) as materialized:
        repo = materialized.repository_path
        if (
            materialized.source_tree != expected_tree
            or materialized.file_count != 1
            or materialized.total_bytes != len(BEFORE)
            or _git(repo, deadline, "ls-files").splitlines() != [b"file.txt"]
            or (repo / "file.txt").read_bytes() != BEFORE
        ):
            raise SnapshotCodexProofError("fake_codex_snapshot_unexpected")
        _git(repo, deadline, "checkout", "-qb", "main")
        _git(repo, deadline, "commit", "-qm", "Synthetic baseline from fixture-asserted Git tree")
        baseline = _git(repo, deadline, "rev-parse", "HEAD").decode()
        if _git(repo, deadline, "rev-parse", "HEAD^{tree}").decode() != expected_tree:
            raise SnapshotCodexProofError("fake_codex_baseline_mismatch")
        if (repo / "file.txt").read_bytes() == AFTER:
            raise SnapshotCodexProofError("fake_codex_before_assertion_unexpected")
        private_home = work_root / ("private-home-" + uuid4().hex)
        private_home.mkdir(mode=0o700)
        try:
            request = CliRequest(
                "codex_cli",
                Path("/opt/codex-cli/node_modules/.bin/codex"),
                repo,
                private_home,
                PROMPT,
                45,
                65536,
            )
            result = invoke_file_codex(request, image_id, fixture_image_id)
        finally:
            private_home.rmdir()
        if (
            result.status != "done"
            or result.provider_session_id is None
            or result.tokens_used is None
            or result.cost_usd is not None
            or (repo / "file.txt").read_bytes() != AFTER
            or _git(repo, deadline, "rev-parse", "HEAD").decode() != baseline
            or _run(("git", "status", "--porcelain", "--untracked-files=all"), repo, deadline).stdout
            != b" M file.txt\n"
        ):
            raise SnapshotCodexProofError("fake_codex_edit_unproved")
        before_blob = hashlib.sha1(f"blob {len(BEFORE)}\0".encode() + BEFORE).hexdigest()  # noqa: S324
        after_blob = hashlib.sha1(f"blob {len(AFTER)}\0".encode() + AFTER).hexdigest()  # noqa: S324
        expected_diff = (
            "diff --git a/file.txt b/file.txt\n"
            f"index {before_blob[:7]}..{after_blob[:7]} 100644\n"
            "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n"
            "-synthetic HTTPS fixture only\n+synthetic HTTPS fixture repaired\n"
        ).encode()
        diff = _git(repo, deadline, "diff", "--", "file.txt") + b"\n"
        if diff != expected_diff:
            raise SnapshotCodexProofError("fake_codex_diff_unproved")
        return SnapshotCodexProof(
            expected_tree,
            snapshot_sha256,
            baseline,
            "failed",
            "passed",
            hashlib.sha256(diff).hexdigest(),
            result.provider_session_id,
            result.tokens_used,
            result.cost_usd,
            result.sanitized_log,
        )
