"""Fake-process tests for the standalone Claude Code adapter."""

import sys
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from runner import claude_code
from runner.claude_code import ClaudeRequest, build_argv, run_claude


def request(tmp_path: Path, body: str, *, seconds: int = 2, output_bytes: int = 4096) -> ClaudeRequest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "fake-claude"
    executable.write_text(f"#!{sys.executable}\n{body}\n")
    executable.chmod(0o700)
    repository = tmp_path / "repo"
    repository.mkdir()
    private_home = tmp_path / "private-home"
    private_home.mkdir(mode=0o700)
    return ClaudeRequest(
        executable,
        repository,
        private_home,
        uuid4(),
        "test-model",
        "Inspect synthetic repository only",
        seconds,
        output_bytes,
        3,
    )


def test_read_only_success_records_owned_session_and_usage(tmp_path: Path) -> None:
    body = """
import json, os, sys
assert '--print' in sys.argv
assert '--restricted' in sys.argv and '--bare' in sys.argv
assert sys.argv[sys.argv.index('--tools') + 1] == 'Read,Glob,Grep'
assert sys.argv[sys.argv.index('--permission-prompts') + 1] == 'none'
assert '--no-session-persistence' in sys.argv
assert 'Inspect synthetic repository' not in ' '.join(sys.argv)
assert os.environ['HOME'].endswith('private-home')
assert os.environ['CLAUDE_CONFIG_DIR'].endswith('private-home/claude')
assert 'Inspect synthetic repository' in sys.stdin.read()
session = sys.argv[sys.argv.index('--session-id') + 1]
print(json.dumps({'type':'system','session_id':session}), flush=True)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read'}]}}), flush=True)
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':session,
                  'usage':{'input_tokens':2,'output_tokens':3,'cache_creation_input_tokens':4,
                           'cache_read_input_tokens':5},'total_cost_usd':0.01}), flush=True)
"""
    item = request(tmp_path, body)
    argv = build_argv(item)
    assert "--session-id" in argv and str(item.session_id) in argv
    assert "--disallowedTools" in argv and "mcp__*" in argv
    outcome = run_claude(item, environment={})
    assert (outcome.status, outcome.reason_code, outcome.session_id) == (
        "done",
        "provider_completed",
        str(item.session_id),
    )
    assert (outcome.tokens_used, outcome.cost_usd) == (14, 0.01)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("print('not json', flush=True)", "invalid_provider_stream"),
        ("print('x' * 8192, flush=True)", "provider_output_limit"),
        ("import sys; sys.exit(2)", "provider_exit_nonzero"),
        ("import time; time.sleep(10)", "provider_timeout"),
        (
            "import json,sys; s=sys.argv[sys.argv.index('--session-id')+1]; "
            "print(json.dumps({'type':'approval_needed','session_id':s}),flush=True)",
            "invalid_provider_stream",
        ),
        (
            "import json,sys; s=sys.argv[sys.argv.index('--session-id')+1]; "
            "print(json.dumps({'type':'assistant','message':{'content':"
            "[{'type':'tool_use','name':'Bash'}]}}),flush=True)",
            "invalid_provider_stream",
        ),
        (
            "import json; print(json.dumps({'type':'result','subtype':'success',"
            "'is_error':False,'session_id':'foreign'}),flush=True)",
            "invalid_provider_stream",
        ),
    ],
)
def test_uncertain_outcomes_fail_closed(tmp_path: Path, body: str, reason: str) -> None:
    item = request(tmp_path, body, seconds=1, output_bytes=1024)
    outcome = run_claude(item, environment={})
    assert (outcome.status, outcome.reason_code) == ("unknown", reason)


def test_cancellation_is_unknown_and_kills_fake_process(tmp_path: Path) -> None:
    item = request(tmp_path, "import sys,time; sys.stdin.read(); time.sleep(10)")
    cancel = Event()
    cancel.set()
    outcome = run_claude(item, environment={}, cancel=cancel)
    assert (outcome.status, outcome.reason_code) == ("unknown", "cancelled_before_terminal")


def test_unwritable_stdin_obeys_wall_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = request(tmp_path, "import time; time.sleep(10)", seconds=1)

    def blocked_write(_descriptor: int, _data: bytes) -> int:
        raise BlockingIOError()

    monkeypatch.setattr(claude_code, "_write_nonblocking", blocked_write)
    started = time.monotonic()
    outcome = run_claude(item, environment={})
    assert (outcome.status, outcome.reason_code) == ("unknown", "provider_timeout")
    assert time.monotonic() - started < 3


def test_missing_terminal_and_bad_usage_do_not_pass(tmp_path: Path) -> None:
    item = request(tmp_path, "import sys; sys.stdin.read(); print('{}',flush=True)")
    assert run_claude(item, environment={}).reason_code == "invalid_provider_stream"
    item = request(
        tmp_path / "other",
        "import json,sys; s=sys.argv[sys.argv.index('--session-id')+1]; "
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'session_id':s,'usage':{'input_tokens':'bad','output_tokens':1}}),flush=True)",
    )
    assert run_claude(item, environment={}).reason_code == "invalid_provider_stream"


def test_invalid_private_home_and_repository_rejected(tmp_path: Path) -> None:
    item = request(tmp_path, "print('unused')")
    item.private_home.chmod(0o755)
    with pytest.raises(ValueError, match="invalid Claude request"):
        build_argv(item)
