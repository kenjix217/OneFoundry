"""Offline contract checks for coding CLI adapters using fake executables."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from runner.external_cli import AdapterId, CliRequest, build_argv, run_cli
from runner.provider_progress import AGENT_UPDATE_TEXT, ProviderProgress


def _request(
    tmp_path: Path, adapter: AdapterId, script: str, *, seconds: int = 2, output_bytes: int = 4096
) -> CliRequest:
    executable = tmp_path / "fake-provider"
    executable.write_text(f"#!{sys.executable}\n{script}\n")
    executable.chmod(0o700)
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    private_home = tmp_path / "private-home"
    private_home.mkdir(exist_ok=True, mode=0o700)
    return CliRequest(
        adapter,
        executable,
        repository,
        private_home,
        "Fix calculator without sending SECRET_PROMPT to argv",
        seconds,
        output_bytes,
    )


def test_codex_success_has_fixed_argv_and_private_home(tmp_path: Path) -> None:
    script = """
import json, os, sys
assert 'SECRET_PROMPT' not in ' '.join(sys.argv)
assert os.environ['HOME'].endswith('private-home')
assert os.environ['CODEX_HOME'].endswith('private-home/codex')
assert os.environ['CLAUDE_CONFIG_DIR'].endswith('private-home/claude')
assert 'SECRET_PROMPT' in sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':'session-1'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message',
                  'text':'I found the failing test. Next I will repair it.'}}))
print(json.dumps({'type':'item.completed','item':{'type':'command_execution',
                  'command':'git diff --check'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}))
"""
    request = _request(tmp_path, "codex_cli", script)
    argv = build_argv(request)
    assert argv[1:5] == ["--ask-for-approval", "never", "exec", "--json"]
    assert "danger-full-access" not in argv
    assert "--ignore-user-config" in argv
    assert "--ephemeral" in argv
    result = run_cli(request, environment={})
    assert (result.status, result.reason_code, result.provider_session_id) == (
        "done",
        "provider_completed",
        "session-1",
    )
    assert result.tokens_used == 15
    assert result.cost_usd is None
    assert b"SECRET_PROMPT" not in result.sanitized_log
    assert [item.kind for item in result.progress] == ["agent_update", "tool_activity"]
    assert result.progress[0].text == AGENT_UPDATE_TEXT
    assert result.progress[1].command == "git diff --check"


def test_claude_success_and_cost_are_parsed(tmp_path: Path) -> None:
    script = """
import json, sys
assert 'SECRET_PROMPT' not in ' '.join(sys.argv)
assert 'SECRET_PROMPT' in sys.stdin.read()
print(json.dumps({'type':'system','session_id':'session-2'}))
print(json.dumps({'type':'assistant','message':{'content':[
    {'type':'thinking','thinking':'PRIVATE COT'},
    {'type':'text','text':'I found the empty input case and will update it.'},
    {'type':'tool_use','name':'Edit','input':{'new_string':'SECRET CODE'}}]}}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'session-2',
                  'usage':{'input_tokens':2,'output_tokens':4,'cache_creation_input_tokens':7,
                           'cache_read_input_tokens':11},'total_cost_usd':0.01}))
"""
    request = _request(tmp_path, "claude_code", script)
    argv = build_argv(request)
    assert "--bare" in argv and "--restricted" in argv
    assert "--strict-mcp-config" in argv
    assert "--permission-prompts" in argv and "none" in argv
    assert argv[argv.index("--tools") + 1] == "Edit,Read,Write"
    assert argv[argv.index("--allowed-tools") + 1] == "Edit,Read,Write"
    assert "Bash" not in argv
    result = run_cli(request, environment={})
    assert (result.status, result.provider_session_id, result.tokens_used, result.cost_usd) == (
        "done",
        "session-2",
        24,
        0.01,
    )
    assert [item.text for item in result.progress] == [
        AGENT_UPDATE_TEXT,
        "Requested a project file edit.",
    ]
    assert "PRIVATE COT" not in repr(result.progress)
    assert "SECRET CODE" not in repr(result.progress)


def test_progress_callback_runs_before_cli_exits(tmp_path: Path) -> None:
    marker = tmp_path / "progress-received"
    os.mkfifo(marker)
    script = f"""
import json, pathlib, sys
sys.stdin.read()
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message',
                  'text':'I found the failing test and will repair it.'}}}}), flush=True)
marker = pathlib.Path({str(marker)!r})
with marker.open() as signal: assert signal.read() == 'seen'
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':2,'output_tokens':3}}}}), flush=True)
"""
    request = _request(tmp_path, "codex_cli", script)
    seen: list[str] = []

    def observe(item: ProviderProgress) -> None:
        seen.append(item.text)
        with marker.open("w") as signal:
            signal.write("seen")

    result = run_cli(replace(request, on_progress=observe), environment={})
    assert result.status == "done"
    assert seen == [AGENT_UPDATE_TEXT]


@pytest.mark.parametrize(
    "cache",
    [
        "'cache_read_input_tokens': '3'",
        "'cache_creation': {'ephemeral_5m_input_tokens': 3}",
        "'cache_read_input_tokens': 2147483647",
    ],
)
def test_claude_ambiguous_cache_usage_is_unknown(tmp_path: Path, cache: str) -> None:
    script = f"""
import json, sys
sys.stdin.read()
print(json.dumps({{'type':'result','subtype':'success','is_error':False,
                  'usage':{{'input_tokens':2,'output_tokens':4,{cache}}}}}))
"""
    result = run_cli(_request(tmp_path, "claude_code", script), environment={})
    assert result.status == "done"
    assert result.tokens_used is None


def test_codex_token_total_overflow_is_unknown(tmp_path: Path) -> None:
    script = """
import json, sys
sys.stdin.read()
print(json.dumps({'type':'turn.completed',
                  'usage':{'input_tokens':2147483647,'output_tokens':1}}))
"""
    result = run_cli(_request(tmp_path, "codex_cli", script), environment={})
    assert result.status == "done"
    assert result.tokens_used is None


@pytest.mark.parametrize(
    ("script", "seconds", "output_bytes", "expected"),
    [
        ("import sys; sys.exit(2)", 2, 4096, "provider_exit_nonzero"),
        ("print('not json')", 2, 4096, "provider_terminal_unproved"),
        ("print('x' * 8192)", 2, 512, "provider_output_limit"),
        ("import time; time.sleep(10)", 1, 4096, "provider_timeout"),
    ],
)
def test_uncertain_outcomes_never_report_success(
    tmp_path: Path,
    script: str,
    seconds: int,
    output_bytes: int,
    expected: str,
) -> None:
    result = run_cli(
        _request(tmp_path, "codex_cli", script, seconds=seconds, output_bytes=output_bytes), environment={}
    )
    assert (result.status, result.reason_code) == ("unknown", expected)
    assert result.sanitized_log == f"adapter=codex_cli outcome={expected}\n".encode()


def test_invalid_adapter_and_host_home_rejected(tmp_path: Path) -> None:
    request = _request(tmp_path, cast(AdapterId, "unregistered"), "print('hello')")
    with pytest.raises(ValueError, match="unsupported_adapter"):
        build_argv(request)
    request = CliRequest(
        "codex_cli", request.executable, request.repository, Path.home(), request.prompt, 2, 4096
    )
    with pytest.raises(ValueError, match="invalid_cli_request"):
        run_cli(request, environment={})
