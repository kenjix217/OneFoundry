"""Gateway browser intent reconciles only one exact host claim and receipt."""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.browser_static_fixtures import StaticFixtureStore
from onefoundry.browser_static_jobs import BrowserStaticJobStore
from onefoundry.gateway_errors import Conflict
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from runner.browser_static_job_spool import BrowserStaticJobSpool
from runner.browser_static_supervisor import StaticBrowserReceipt
from tests.test_browser_static_fixtures_postgres import proposal
from tests.test_role_tool_admission_postgres import _run

pytest_plugins = ["tests.test_browser_static_fixtures_postgres"]

IMAGE = "sha256:" + "a" * 64


def _jobs(
    gateway: ToolGateway, fixtures: StaticFixtureStore, tmp_path: Path
) -> tuple[BrowserStaticJobStore, BrowserStaticJobSpool]:
    root = tmp_path / "browser-spool"
    root.mkdir(mode=0o700)
    spool = BrowserStaticJobSpool(root)
    return BrowserStaticJobStore(gateway, fixtures, spool, IMAGE, 30), spool


def test_intent_claim_receipt_and_reconciliation(
    client: TestClient,
    gateway: ToolGateway,
    store: StaticFixtureStore,
    tmp_path: Path,
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    key = uuid4()
    queued = jobs.launch(project_id, run_id, fixture["id"], key)
    assert queued["status"] == "queued"
    assert jobs.launch(project_id, run_id, fixture["id"], key)["id"] == queued["id"]
    with pytest.raises(Conflict, match="no host claim"):
        jobs.reconcile(project_id, run_id, queued["id"])

    def inspect(target: object, *, html: bytes, css: bytes, image: str) -> StaticBrowserReceipt:
        assert html == proposal().html.encode() and css == proposal().css.encode()
        assert image == IMAGE
        approved = store.approved(project_id, run_id, fixture["id"])
        assert target == approved.target
        return StaticBrowserReceipt(
            approved.target.binding_digest,
            approved.source_sha256,
            "Fixture",
            "Approved page",
            approved.target.approved_url,
            2,
            IMAGE,
        )

    observed = spool.process_job(queued["id"], inspect=inspect)
    assert observed is not None and observed["status"] == "done"
    done = jobs.reconcile(project_id, run_id, queued["id"])
    assert done["status"] == "done"
    evidence = jobs.evidence(project_id, run_id, queued["id"])
    assert evidence["title"] == "Fixture"
    assert evidence["text"] == "Approved page"
    assert evidence["blocked_request_count"] == 2
    assert "html" not in evidence and "css" not in evidence
    assert not (spool.root / "sources" / f"{queued['id']}.bin").exists()
    assert done["receipt_sha256"] == spool.read_receipt(queued["id"])[1]  # type: ignore[index]
    assert jobs.reconcile(project_id, run_id, queued["id"])["status"] == "done"
    assert spool.process_job(queued["id"], inspect=inspect) is None


def test_claimed_failure_reconciles_unknown_without_retry(
    client: TestClient,
    gateway: ToolGateway,
    store: StaticFixtureStore,
    tmp_path: Path,
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    queued = jobs.launch(project_id, run_id, fixture["id"], uuid4())

    def fail(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        raise RuntimeError("private failure")

    receipt = spool.process_job(queued["id"], inspect=fail)
    assert receipt is not None and receipt["status"] == "unknown"
    unknown = jobs.reconcile(project_id, run_id, queued["id"])
    assert unknown["status"] == "unknown"
    assert not (spool.root / "sources" / f"{queued['id']}.bin").exists()
    assert spool.process_job(queued["id"], inspect=fail) is None


def test_mismatched_host_receipt_cannot_complete_job(
    client: TestClient,
    gateway: ToolGateway,
    store: StaticFixtureStore,
    tmp_path: Path,
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    queued = jobs.launch(project_id, run_id, fixture["id"], uuid4())

    def fail(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        raise RuntimeError("private failure")

    spool.process_job(queued["id"], inspect=fail)
    path = spool.root / "receipts" / f"{queued['id']}.json"
    changed = json.loads(path.read_text())
    changed["binding_digest"] = "0" * 64
    path.write_text(json.dumps(changed))
    with pytest.raises(Conflict, match="does not match"):
        jobs.reconcile(project_id, run_id, queued["id"])
    assert jobs._row(project_id, run_id, queued["id"], lock=False)["status"] == "queued"


def test_stale_claim_without_receipt_becomes_unknown_without_retry(
    client: TestClient,
    gateway: ToolGateway,
    store: StaticFixtureStore,
    tmp_path: Path,
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    queued = jobs.launch(project_id, run_id, fixture["id"], uuid4())
    claim = spool.root / "claims" / f"{queued['id']}.claim"
    spool._new(claim, queued["request_sha256"].encode())
    then = time.time() - 31
    os.utime(claim, (then, then))
    unknown = jobs.reconcile(project_id, run_id, queued["id"])
    assert unknown["status"] == "unknown"
    assert unknown["reason_code"] == "claimed_receipt_missing"
    assert jobs.reconcile(project_id, run_id, queued["id"])["status"] == "unknown"


def test_no_claim_timeout_terminalizes_without_retry(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    queued = jobs.launch(project_id, run_id, fixture["id"], uuid4())
    with pytest.raises(Conflict, match="no host claim"):
        jobs.reconcile(project_id, run_id, queued["id"], now=queued["created_at"] + timedelta(seconds=29))
    unknown = jobs.reconcile(
        project_id, run_id, queued["id"], now=queued["created_at"] + timedelta(seconds=30)
    )
    assert unknown["status"] == "unknown"
    assert unknown["reason_code"] == "host_claim_missing"
    assert spool.fence_unclaimed(queued["id"], queued["request_sha256"]) is True
    assert (
        spool.process_job(queued["id"], inspect=lambda *_args, **_kwargs: pytest.fail("late launch")) is None
    )
    assert jobs.reconcile(project_id, run_id, queued["id"])["status"] == "unknown"
    assert not (spool.root / "sources" / f"{queued['id']}.bin").exists()
    with pytest.raises(Conflict, match="evidence is not available"):
        jobs.evidence(project_id, run_id, queued["id"])


def test_host_claim_winning_timeout_race_stays_queued(
    client: TestClient,
    gateway: ToolGateway,
    store: StaticFixtureStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    queued = jobs.launch(project_id, run_id, fixture["id"], uuid4())
    actual_fence = spool.fence_unclaimed

    def host_claim_first(job_id: object, request_sha256: str) -> bool:
        spool._new(spool._path("claims", queued["id"], "claim"), request_sha256.encode())
        return actual_fence(queued["id"], request_sha256)

    monkeypatch.setattr(spool, "fence_unclaimed", host_claim_first)
    with pytest.raises(Conflict, match="claimed"):
        jobs.reconcile(project_id, run_id, queued["id"], now=queued["created_at"] + timedelta(seconds=30))
    assert jobs._row(project_id, run_id, queued["id"], lock=False)["status"] == "queued"
    assert spool.claimed(queued["id"], queued["request_sha256"]) is True


def test_partial_claim_stays_pending_then_reconciles_unknown(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    jobs, spool = _jobs(gateway, store, tmp_path)
    queued = jobs.launch(project_id, run_id, fixture["id"], uuid4())
    claim = spool.root / "claims" / f"{queued['id']}.claim"
    spool._new(claim, b"")
    with pytest.raises(Conflict, match="claimed"):
        jobs.reconcile(project_id, run_id, queued["id"])
    assert jobs._row(project_id, run_id, queued["id"], lock=False)["status"] == "queued"
    then = time.time() - 31
    os.utime(claim, (then, then))
    unknown = jobs.reconcile(project_id, run_id, queued["id"])
    assert unknown["status"] == "unknown"
    assert unknown["reason_code"] == "claimed_receipt_missing"
    assert (
        spool.process_job(queued["id"], inspect=lambda *_args, **_kwargs: pytest.fail("late launch")) is None
    )


def test_runtime_role_browser_grants(gateway: ToolGateway) -> None:
    with gateway.engine.connect() as connection:
        rights = connection.execute(
            text(
                "SELECT has_table_privilege(current_user,'browser_static_fixtures','SELECT'), "
                "has_table_privilege(current_user,'browser_static_fixtures','INSERT'), "
                "has_table_privilege(current_user,'browser_static_fixtures','DELETE'), "
                "has_column_privilege(current_user,'browser_static_fixtures','id','UPDATE'), "
                "has_column_privilege(current_user,'browser_static_fixtures','html','UPDATE'), "
                "has_table_privilege(current_user,'browser_static_jobs','SELECT'), "
                "has_table_privilege(current_user,'browser_static_jobs','INSERT'), "
                "has_column_privilege(current_user,'browser_static_jobs','status','UPDATE'), "
                "has_column_privilege(current_user,'browser_static_jobs','inspection_text','UPDATE'), "
                "has_column_privilege(current_user,'browser_static_jobs','image_id','UPDATE'), "
                "has_table_privilege(current_user,'browser_static_jobs','DELETE')"
            )
        ).one()
    assert tuple(rights) == (True, True, False, True, False, True, True, True, True, False, False)
