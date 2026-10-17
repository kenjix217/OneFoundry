"""Host setup admission, one execution, owner failure and interrupted-effect handling."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from onefoundry.capability_setup_host_contract import HostSetupJob
from onefoundry.repository_code_contract import digest
from runner.capability_setup_settings import HostSetupSettings
from runner.capability_setup_worker import SetupHttp, process_setup
from tests.test_role_repository_file_unit import _git


class Http:
    def __init__(self, revision: str) -> None:
        self.revision, self.source = revision, str(uuid4())
        self.acquired = True
        self.calls: list[str] = []

    def post(self, job: HostSetupJob, action: str) -> dict[str, Any]:
        self.calls.append(action)
        return {"acquired": self.acquired, "source_id": self.source, "revision": self.revision}


def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "README.md").write_text("Committed reference.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    project = uuid4()
    settings = HostSetupSettings.model_validate(
        {
            "repositories": [{"alias": "repo", "path": str(repo), "projects": [str(project)]}],
            "git_timeout_seconds": 3,
            "restart_timeout_seconds": 5,
        }
    )
    offer = settings.catalog().repositories[0]
    binding = {
        "source": offer.model_dump(mode="json"),
        "request": {"repository_alias": "repo", "revision": offer.revision},
    }
    target, content = "Set up access", "Mount the selected repository read-only."
    job = HostSetupJob(
        job_id=uuid4(),
        project_id=project,
        binding=binding,
        target=target,
        content=content,
        proposal_digest=digest({"binding": binding, "target": target, "content": content}),
    )
    spool = tmp_path / "spool"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    path = spool / "jobs" / f"{job.job_id}.json"
    path.write_text(job.model_dump_json())
    home = tmp_path / "home"
    (home / "data/config").mkdir(parents=True)
    env = home / "data/config/.env"
    env.write_text("LOCAL_SOURCE_ROOT=/sources\n")
    env.chmod(0o600)
    monkeypatch.setattr("scripts.onefoundry_update.read_install", lambda _: (home, {"project": "fixture"}))
    monkeypatch.setattr("scripts.onefoundry_update.health", lambda *_: None)
    return path, spool, home, settings, Http(offer.revision), repo


def test_mount_once_and_keep_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spool, home, settings, http, repo = fixture(tmp_path, monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def compose(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        entered.set()
        assert finish.wait(5)

    monkeypatch.setattr("scripts.onefoundry_update.compose", compose)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_setup, path, spool, home, settings, http)
        assert entered.wait(5)
        assert pool.submit(process_setup, path, spool, home, settings, http).result() is None
        finish.set()
        result = first.result()
    assert result and result["status"] == "configured"
    mount = json.loads((home / "data/config/local-sources.compose.json").read_text())
    assert mount["services"]["app"]["volumes"] == [
        {"type": "bind", "source": str(repo), "target": "/sources/repo", "read_only": True}
    ]
    assert calls[0][1]["timeout"] == 5 and len(calls) == 1
    assert process_setup(path, spool, home, settings, http) is None
    assert len(calls) == 1 and http.calls.count("register") == 1


@pytest.mark.parametrize("failure", ["not_approved", "drift", "interrupted", "restart", "register"])
def test_setup_failure_is_observed_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path, spool, home, settings, http, repo = fixture(tmp_path, monkeypatch)
    calls = []

    def compose(*args: Any, **kwargs: Any) -> None:
        calls.append(args)
        if failure == "restart" and len(calls) == 1:
            raise RuntimeError("owner failed")

    monkeypatch.setattr("scripts.onefoundry_update.compose", compose)
    if failure == "not_approved":
        http.acquired = False
    elif failure == "drift":
        (repo / "README.md").write_text("Different revision.")
        _git(repo, "commit", "-qam", "changed")
    elif failure == "interrupted":
        (spool / "claims" / f"{path.stem}.claim").write_text("interrupted")
    elif failure == "register":
        http.revision = "a" * 40
    result = process_setup(path, spool, home, settings, http)
    if failure == "not_approved":
        assert result is None and not list((spool / "claims").glob("*.claim"))
    else:
        assert result and result["status"] == (
            "unknown" if failure in {"interrupted", "register"} else "failed"
        )
        before = len(calls)
        assert process_setup(path, spool, home, settings, http) is None
        assert len(calls) == before
    if failure == "restart":
        assert len(calls) == 2 and not (home / "data/config/local-sources.compose.json").exists()
    if failure in {"interrupted", "register"}:
        assert "release" not in http.calls


@pytest.mark.parametrize(
    "origin", ["https://example.com", "http://localhost/", "http://x@localhost", "file:///tmp"]
)
def test_setup_requires_exact_loopback(origin: str) -> None:
    with pytest.raises(ValueError):
        SetupHttp(origin, 3)


def test_catalog_rejects_symlink_and_duplicate_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _path, _spool, _home, settings, _http, repo = fixture(tmp_path, monkeypatch)
    value = settings.model_dump()
    value["repositories"] *= 2
    with pytest.raises(ValueError):
        HostSetupSettings.model_validate(value)
    link = repo.parent / "link"
    link.symlink_to(repo)
    settings.repositories[0].path = link
    with pytest.raises(ValueError):
        settings.catalog()
