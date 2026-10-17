"""Bounded terminal observations without retaining coding commands or their output."""

from __future__ import annotations

import json
from typing import Any

from runner.external_cli import CliResult, _parse_events
from runner.repository_provider_diagnostics import valid_denial

# This observer runs inside the existing isolated client. Large command output
# is drained, not accumulated in spawnSync's maxBuffer or copied into receipts.
CLI_OBSERVER = r"""
async function observeCli(executable, argv, options) {
  return await new Promise(resolve => {
    let pending = '', discard = false, stdoutBytes = 0, stderrBytes = 0;
    let session = null, terminal = null, terminals = 0, timedOut = false, spawnError = false;
    const child = cp.spawn(executable, argv, {cwd: options.cwd, env: options.env,
      stdio: ['pipe', 'pipe', 'pipe']});
    const timer = setTimeout(() => {timedOut = true; child.kill('SIGKILL');}, options.wallMilliseconds);
    function line(value) {
      let event; try {event = JSON.parse(value);} catch {return;}
      if (!event || typeof event !== 'object') return;
      if (event.type === 'thread.started' && typeof event.thread_id === 'string' &&
          /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(event.thread_id)) session = event.thread_id;
      if (event.type === 'turn.completed' || event.type === 'turn.failed') {
        terminals++;
        const usage = event.usage || {};
        terminal = {type:event.type};
        if (Number.isSafeInteger(usage.input_tokens) && usage.input_tokens >= 0 &&
            Number.isSafeInteger(usage.output_tokens) && usage.output_tokens >= 0)
          terminal.usage = {input_tokens:usage.input_tokens, output_tokens:usage.output_tokens};
      }
    }
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdoutBytes += Buffer.byteLength(chunk);
      for (const part of chunk.split(/(?<=\n)/)) {
        if (!discard) {
          pending += part;
          if (Buffer.byteLength(pending) > options.recordLimit) {pending = ''; discard = true;}
        }
        if (part.endsWith('\n')) {if (!discard) line(pending); pending = ''; discard = false;}
      }
    });
    child.stderr.on('data', chunk => {stderrBytes += chunk.length;});
    child.stdin.on('error', () => {});
    child.on('error', () => {spawnError = true;});
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      if (pending && !discard) line(pending);
      if (session) process.stdout.write(JSON.stringify({type:'thread.started',thread_id:session})+'\n');
      if (terminal && terminals === 1) process.stdout.write(JSON.stringify(terminal)+'\n');
      const observation = {type:'repository.cli', exit_code:code, signaled:signal !== null,
        timed_out:timedOut, spawn_error:spawnError, terminal_count:terminals,
        stdout_bytes:stdoutBytes, stderr_bytes:stderrBytes};
      process.stdout.write(JSON.stringify(observation)+'\n');
      resolve();
    });
    child.stdin.end(options.input);
  });
}
"""


def result(raw: bytes) -> CliResult:
    """A stopped client without a single terminal provider turn stays unknown."""
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    observations = [item for item in records if item.get("type") == "repository.cli"]
    done, session, tokens, cost = _parse_events("codex_cli", raw)
    if len(observations) != 1:
        return CliResult("unknown", "maintenance_cli_observation_missing", session, tokens, cost, b"")
    observation = observations[0]
    valid = (
        set(observation)
        == {
            "type",
            "exit_code",
            "signaled",
            "timed_out",
            "spawn_error",
            "terminal_count",
            "stdout_bytes",
            "stderr_bytes",
        }
        and (observation["exit_code"] is None or type(observation["exit_code"]) is int)
        and all(type(observation[key]) is bool for key in ("signaled", "timed_out", "spawn_error"))
        and all(
            type(observation[key]) is int and observation[key] >= 0
            for key in ("terminal_count", "stdout_bytes", "stderr_bytes")
        )
    )
    if not valid:
        return CliResult("unknown", "maintenance_cli_observation_invalid", session, tokens, cost, b"")
    log = (json.dumps(observation, sort_keys=True) + "\n").encode()
    brokers = [item for item in records if item.get("type") == "repository.broker"]
    denial = None
    if len(brokers) == 1:
        broker = brokers[0]
        keys = {
            "type",
            "forwarded_requests",
            "request_bytes",
            "response_bytes",
            "last_denial",
        }
        if (
            set(broker) in (keys, keys | {"retried_requests"})
            and all(
                type(broker[key]) is int and 0 <= broker[key] <= 1_600_000_000
                for key in ("forwarded_requests", "request_bytes", "response_bytes")
            )
            and type(broker.get("retried_requests", 0)) is int
            and 0 <= broker.get("retried_requests", 0) <= 100
        ):
            candidate = broker["last_denial"]
            if candidate is None or valid_denial(candidate):
                denial = candidate
                log += (json.dumps(broker, sort_keys=True) + "\n").encode()
    if (
        done
        and session
        and observation["terminal_count"] == 1
        and observation["exit_code"] == 0
        and not any(observation[key] for key in ("signaled", "timed_out", "spawn_error"))
    ):
        return CliResult("done", "completed", session, tokens, cost, log)
    reason = "maintenance_provider_terminal_unproved"
    if observation["timed_out"]:
        reason = "maintenance_cli_timeout"
    elif observation["spawn_error"]:
        reason = "maintenance_cli_spawn_failed"
    elif any(item.get("type") == "turn.failed" for item in records):
        reason = denial or "maintenance_provider_turn_failed"
    return CliResult("unknown", reason, session, tokens, cost, log)
