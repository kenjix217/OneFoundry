"""A new package must not inherit an unrelated package's role-tool grant."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock, get_ident, local
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, create_engine, text

from onefoundry import (
    capability_setup,
    coordination_profiles,
    gateway_extension_identity,
    gateway_extension_store,
)
from onefoundry.gateway_errors import Conflict
from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.gateway_extension_schemas import PackageChange
from onefoundry.main import create_app
from onefoundry.profile_hierarchy import lock_project as lock_hierarchy_project
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from tests.test_coordination_profiles_postgres import _env as profile_env
from tests.test_coordination_profiles_postgres import _propose as propose_profile
from tests.test_coordination_profiles_postgres import _setup as setup_profile_run
from tests.test_gateway_extension_lifecycle_postgres import _change_package
from tests.test_gateway_extension_remote_postgres import ORIGIN, _post
from tests.test_self_extension_postgres import payload

OLD_KEY = "identity-probe-19918"
NEW_KEY = "identity-probe-20485"
TOOL_LOCAL_ID = "lookup"
pytest_plugins = ("tests.test_capability_setup_postgres",)


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[TestClient, ToolGateway]]:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        extension_allowed_origins=["https://api.example.test"],
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            yield client, gateway
    finally:
        engine.dispose()


def _package(
    client: TestClient,
    base: str,
    key: str,
    *,
    tool_ids: tuple[str, ...] = (TOOL_LOCAL_ID,),
    version: str = "1.0.0",
) -> dict[str, object]:
    return _post(
        client,
        f"{base}/extensions/packages",
        {
            "manifest": {
                "schema_version": 1,
                "id": key,
                "version": version,
                "name": key,
                "connections": [
                    {
                        "id": "api",
                        "name": "API",
                        "kind": "http",
                        "operations": [{"id": TOOL_LOCAL_ID, "method": "GET", "path": "/lookup"}],
                    }
                ],
                "tools": [
                    {
                        "id": tool_id,
                        "description": f"Read through {key}",
                        "effect": "read",
                        "runtime": "http",
                        "connection": "api",
                        "operation": TOOL_LOCAL_ID,
                        "parameters": {"type": "object", "additionalProperties": False},
                        "returns": {"type": "object", "additionalProperties": False},
                    }
                    for tool_id in tool_ids
                ],
            },
            "files": {},
        },
    )


def _enable_connection(client: TestClient, base: str, package: dict[str, object]) -> None:
    saved = _post(
        client,
        f"{base}/extensions/connections",
        {
            "package_id": package["id"],
            "template_id": "api",
            "label": "Identity probe",
            "config": {"transport": "streamable_http", "endpoint": "https://api.example.test"},
        },
    )
    enabled = client.patch(
        f"{base}/extensions/connections/{saved['id']}",
        json={"expected_version": 1, "enabled": True},
        headers=ORIGIN,
    )
    assert enabled.status_code == 200, enabled.text


def _run(client: TestClient, gateway: ToolGateway, base: str, project_id: UUID, profile_id: UUID) -> UUID:
    order = _post(
        client,
        f"{base}/work-orders",
        {"title": "Identity probe", "instructions": "Check admitted tools", "profile_id": str(profile_id)},
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready", {})
    return UUID(str(gateway.start_synthetic_text(project_id, UUID(str(order["id"])))["run_id"]))


def test_unrelated_package_cannot_inherit_revoked_package_tool_grant(
    setup: tuple[TestClient, ToolGateway],
) -> None:
    client, gateway = setup
    # Both IDs are valid package slugs and collide in the current eight-hex prefix.
    assert (
        hashlib.sha256(OLD_KEY.encode()).hexdigest()[:8] == hashlib.sha256(NEW_KEY.encode()).hexdigest()[:8]
    )
    project_id = UUID(str(_post(client, "/api/projects", {"name": f"Identity probe {uuid4()}"})["id"]))
    base = f"/api/projects/{project_id}"
    profile = next(item for item in client.get(f"{base}/profiles").json() if item["name"] == "Researcher")
    profile_id = UUID(profile["id"])

    old = _package(client, base, OLD_KEY)
    _change_package(client, str(project_id), old, "activate")
    _enable_connection(client, base, old)
    old_tool_id = canonical_tool_id(OLD_KEY, TOOL_LOCAL_ID)
    granted = client.patch(
        f"{base}/profiles/{profile_id}", json={"allowed_tools": [old_tool_id]}, headers=ORIGIN
    )
    assert granted.status_code == 200, granted.text
    old_run_id = _run(client, gateway, base, project_id, profile_id)
    assert old_tool_id in {
        item["id"] for item in gateway.available_role_tools(project_id, old_run_id, profile_id)
    }

    _change_package(client, str(project_id), old, "revoke")
    assert old_tool_id not in {
        item["id"] for item in gateway.available_role_tools(project_id, old_run_id, profile_id)
    }
    new = _package(client, base, NEW_KEY)
    assert new["id"] != old["id"]
    assert new["sha256"] != old["sha256"]
    _change_package(client, str(project_id), new, "activate")
    _enable_connection(client, base, new)
    new_tool_id = canonical_tool_id(NEW_KEY, TOOL_LOCAL_ID)
    coordination = client.get(f"{base}/coordination")
    assert coordination.status_code == 200, coordination.text
    registered = coordination.json()
    assert new_tool_id in registered["registered_tools"]
    assert old_tool_id not in registered["registered_tools"]
    assert registered["registered_tool_labels"][new_tool_id] == f"{NEW_KEY} · Lookup ({NEW_KEY}/lookup)"
    assert old_tool_id not in registered["registered_tool_labels"]
    other_id = _post(client, "/api/projects", {"name": f"Other identity probe {uuid4()}"})["id"]
    other_registered = client.get(f"/api/projects/{other_id}/coordination").json()
    assert new_tool_id not in other_registered["registered_tools"]
    assert new_tool_id not in other_registered["registered_tool_labels"]
    unchanged_profile = next(
        item for item in client.get(f"{base}/profiles").json() if item["id"] == str(profile_id)
    )
    assert unchanged_profile["allowed_tools"] == [old_tool_id]

    # The old role grant must not advertise or admit this distinct active package.
    ungranted_run_id = _run(client, gateway, base, project_id, profile_id)
    advertised = {
        item["id"] for item in gateway.available_role_tools(project_id, ungranted_run_id, profile_id)
    }
    admitted_result: dict[str, object] | None = None
    try:
        admitted_result = gateway.invoke_role_tool(
            project_id,
            ungranted_run_id,
            profile_id,
            "ungranted-identity-probe",
            new_tool_id,
            {},
            tool_version=1,
        )
    except ToolDenied:
        pass
    assert new_tool_id not in advertised and admitted_result is None, (
        f"stale grant advertised={new_tool_id in advertised}, invocation={admitted_result}"
    )

    # An explicit grant to the new package should then reach the normal approval wait.
    new_grant = client.patch(
        f"{base}/profiles/{profile_id}", json={"allowed_tools": [new_tool_id]}, headers=ORIGIN
    )
    assert new_grant.status_code == 200, new_grant.text
    granted_run_id = _run(client, gateway, base, project_id, profile_id)
    assert new_tool_id in {
        item["id"] for item in gateway.available_role_tools(project_id, granted_run_id, profile_id)
    }
    pending = gateway.invoke_role_tool(
        project_id, granted_run_id, profile_id, "granted-identity-probe", new_tool_id, {}, tool_version=1
    )
    assert pending["status"] == "approval_required"


def test_activation_rejects_identity_reserved_by_revoked_different_package(
    setup: tuple[TestClient, ToolGateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _gateway = setup
    project_id = str(_post(client, "/api/projects", {"name": f"Reserved identity {uuid4()}"})["id"])
    base = f"/api/projects/{project_id}"
    old = _package(client, base, OLD_KEY)
    _change_package(client, project_id, old, "activate")
    _change_package(client, project_id, old, "revoke")
    assert client.get(f"{base}/extensions/packages/{old['id']}").json()["state"] == "revoked"

    new = _package(client, base, NEW_KEY)
    proposed = _post(
        client,
        f"{base}/extensions/packages/{new['id']}/activate",
        {
            "expected_sha256": new["sha256"],
            "expected_version": new["package_version"],
            "expected_revision": new["revision"],
        },
    )
    original = gateway_extension_identity.canonical_tool_id
    reserved_id = original(OLD_KEY, TOOL_LOCAL_ID)

    def collide_with_revoked(package_key: str, tool_id: str) -> str:
        if package_key == NEW_KEY and tool_id == TOOL_LOCAL_ID:
            return reserved_id
        return original(package_key, tool_id)

    with monkeypatch.context() as patch:
        patch.setattr(gateway_extension_identity, "canonical_tool_id", collide_with_revoked)
        decision = client.post(
            f"{base}/approvals/{proposed['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        )
    assert decision.status_code == 409, decision.text
    assert client.get(f"{base}/extensions/packages/{new['id']}").json()["state"] == "draft"


def test_activation_rejects_two_local_tools_resolving_to_one_canonical_identity(
    setup: tuple[TestClient, ToolGateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _gateway = setup
    project_id = str(_post(client, "/api/projects", {"name": f"Internal alias {uuid4()}"})["id"])
    base = f"/api/projects/{project_id}"
    package_key = "identity-probe-internal-alias"
    package = _package(client, base, package_key, tool_ids=("lookup_left", "lookup_right"))
    proposed = _post(
        client,
        f"{base}/extensions/packages/{package['id']}/activate",
        {
            "expected_sha256": package["sha256"],
            "expected_version": package["package_version"],
            "expected_revision": package["revision"],
        },
    )
    original = gateway_extension_identity.canonical_tool_id
    collapsed_id = original(package_key, "lookup_left")

    def collapse_local_ids(key: str, tool_id: str) -> str:
        if key == package_key and tool_id in {"lookup_left", "lookup_right"}:
            return collapsed_id
        return original(key, tool_id)

    with monkeypatch.context() as patch:
        patch.setattr(gateway_extension_identity, "canonical_tool_id", collapse_local_ids)
        decision = client.post(
            f"{base}/approvals/{proposed['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        )
    assert decision.status_code == 409, decision.text
    assert client.get(f"{base}/extensions/packages/{package['id']}").json()["state"] == "draft"


def test_same_key_upgrade_rejects_different_local_tool_with_reserved_identity(
    setup: tuple[TestClient, ToolGateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _gateway = setup
    project_id = str(_post(client, "/api/projects", {"name": f"Same-key alias {uuid4()}"})["id"])
    base = f"/api/projects/{project_id}"
    key = "identity-probe-same-key-alias"
    old = _package(client, base, key)
    _change_package(client, project_id, old, "activate")
    _change_package(client, project_id, old, "revoke")
    upgraded = _package(client, base, key, tool_ids=("lookup_next",), version="2.0.0")
    proposed = _post(
        client,
        f"{base}/extensions/packages/{upgraded['id']}/activate",
        {
            "expected_sha256": upgraded["sha256"],
            "expected_version": upgraded["package_version"],
            "expected_revision": upgraded["revision"],
        },
    )
    original = gateway_extension_identity.canonical_tool_id
    reserved_id = original(key, TOOL_LOCAL_ID)

    def collide_across_local_ids(package_key: str, tool_id: str) -> str:
        if package_key == key and tool_id == "lookup_next":
            return reserved_id
        return original(package_key, tool_id)

    with monkeypatch.context() as patch:
        patch.setattr(gateway_extension_identity, "canonical_tool_id", collide_across_local_ids)
        decision = client.post(
            f"{base}/approvals/{proposed['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        )
    assert decision.status_code == 409, decision.text
    assert client.get(f"{base}/extensions/packages/{upgraded['id']}").json()["state"] == "draft"


def test_same_key_same_local_tool_upgrade_retains_grant(
    setup: tuple[TestClient, ToolGateway],
) -> None:
    client, gateway = setup
    project_id = UUID(str(_post(client, "/api/projects", {"name": f"Stable upgrade {uuid4()}"})["id"]))
    base = f"/api/projects/{project_id}"
    key = "identity-probe-stable-upgrade"
    old = _package(client, base, key)
    _change_package(client, str(project_id), old, "activate")
    _enable_connection(client, base, old)
    profile = next(item for item in client.get(f"{base}/profiles").json() if item["name"] == "Researcher")
    profile_id = UUID(profile["id"])
    tool_id = canonical_tool_id(key, TOOL_LOCAL_ID)
    grant = client.patch(f"{base}/profiles/{profile_id}", json={"allowed_tools": [tool_id]}, headers=ORIGIN)
    assert grant.status_code == 200, grant.text
    old_run_id = _run(client, gateway, base, project_id, profile_id)
    assert tool_id in {
        item["id"] for item in gateway.available_role_tools(project_id, old_run_id, profile_id)
    }

    _change_package(client, str(project_id), old, "revoke")
    upgraded = _package(client, base, key, version="2.0.0")
    assert upgraded["id"] != old["id"]
    assert upgraded["sha256"] != old["sha256"]
    _change_package(client, str(project_id), upgraded, "activate")
    _enable_connection(client, base, upgraded)
    unchanged_profile = next(
        item for item in client.get(f"{base}/profiles").json() if item["id"] == str(profile_id)
    )
    assert unchanged_profile["allowed_tools"] == [tool_id]
    new_run_id = _run(client, gateway, base, project_id, profile_id)
    assert tool_id in {
        item["id"] for item in gateway.available_role_tools(project_id, new_run_id, profile_id)
    }
    pending = gateway.invoke_role_tool(
        project_id, new_run_id, profile_id, "same-key-upgrade", tool_id, {}, tool_version=1
    )
    assert pending["status"] == "approval_required"


def test_concurrent_same_key_version_approvals_leave_one_active(
    setup: tuple[TestClient, ToolGateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, gateway = setup
    project_id = UUID(str(_post(client, "/api/projects", {"name": f"Version race {uuid4()}"})["id"]))
    base = f"/api/projects/{project_id}"
    key = "identity-probe-version-race"
    packages = (
        _package(client, base, key, version="1.0.0"),
        _package(client, base, key, version="2.0.0"),
    )
    approvals = [
        _post(
            client,
            f"{base}/extensions/packages/{package['id']}/activate",
            {
                "expected_sha256": package["sha256"],
                "expected_version": package["package_version"],
                "expected_revision": package["revision"],
            },
        )["approval_id"]
        for package in packages
    ]
    start = Barrier(3, timeout=10)
    after_active_check = Barrier(2, timeout=5)
    seen_threads: set[int] = set()
    seen_lock = Lock()
    original = gateway_extension_identity.canonical_tool_id

    def rendezvous_after_active_check(package_key: str, tool_id: str) -> str:
        if package_key == key and tool_id == TOOL_LOCAL_ID:
            thread_id = get_ident()
            with seen_lock:
                first_call = thread_id not in seen_threads
                seen_threads.add(thread_id)
            if first_call:
                # A project lock should serialize the second decision, so the
                # first waiter may time out and proceed. Without that lock,
                # both decisions rendezvous after reading no active version.
                try:
                    after_active_check.wait()
                except BrokenBarrierError:
                    pass
        return original(package_key, tool_id)

    def approve(approval_id: object) -> int:
        start.wait()
        try:
            gateway.decide_approval(project_id, UUID(str(approval_id)), ApprovalDecision(decision="approved"))
        except Conflict:
            return 409
        return 200

    with monkeypatch.context() as patch:
        patch.setattr(gateway_extension_identity, "canonical_tool_id", rendezvous_after_active_check)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve, approval_id) for approval_id in approvals]
            start.wait()
            outcomes = [future.result(timeout=20) for future in futures]
    with gateway.engine.connect() as connection:
        active = connection.scalar(
            text(
                "SELECT count(*) FROM extension_packages "
                "WHERE project_id=:p AND package_key=:key AND state='active'"
            ),
            {"p": project_id, "key": key},
        )
    assert sorted(outcomes) == [200, 409] and active == 1, (
        f"approval outcomes={outcomes}, active_versions={active}"
    )


def test_source_run_review_decision_and_new_review_do_not_deadlock(
    setup: tuple[TestClient, ToolGateway], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, gateway = setup
    project_id = UUID(str(_post(client, "/api/projects", {"name": f"Review lock order {uuid4()}"})["id"]))
    base = f"/api/projects/{project_id}"
    profile = next(item for item in client.get(f"{base}/profiles").json() if item["name"] == "Researcher")
    order = _post(
        client,
        f"{base}/work-orders",
        {
            "title": "Shared review source",
            "instructions": "Hold a real source run for two extension reviews",
            "profile_id": profile["id"],
        },
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready", {})
    source_run_id = UUID(str(gateway.start_synthetic_text(project_id, UUID(str(order["id"])))["run_id"]))
    first = _package(client, base, "identity-review-lock-a")
    second = _package(client, base, "identity-review-lock-b")
    first_review = _post(
        client,
        f"{base}/extensions/packages/{first['id']}/activate",
        {
            "expected_sha256": first["sha256"],
            "expected_version": first["package_version"],
            "expected_revision": first["revision"],
            "source_run_id": str(source_run_id),
        },
    )

    decision_locked_order = Event()
    decision_locked_project = Event()
    request_attempted_project = Event()
    request_locked_project = Event()
    role = local()
    original_order_check = ToolGateway._require_unblocked_order
    original_project_lock = gateway_extension_store._lock_project

    def after_order_lock(connection: Connection, owner: UUID, order_id: UUID) -> None:
        original_order_check(connection, owner, order_id)
        if getattr(role, "name", None) == "decision" and order_id == UUID(str(order["id"])):
            decision_locked_order.set()
            if decision_locked_project.is_set():
                assert request_attempted_project.wait(timeout=5), "request did not attempt the project lock"
            else:
                assert request_locked_project.wait(timeout=5), "request did not acquire the project lock"

    def after_project_lock(owner_gateway: ToolGateway, connection: Connection, owner: UUID) -> None:
        if getattr(role, "name", None) == "request":
            request_attempted_project.set()
        original_project_lock(owner_gateway, connection, owner)
        if getattr(role, "name", None) == "request":
            request_locked_project.set()
        elif getattr(role, "name", None) == "decision":
            decision_locked_project.set()

    def approve_first() -> dict[str, object]:
        role.name = "decision"
        return gateway.decide_approval(
            project_id, UUID(str(first_review["approval_id"])), ApprovalDecision(decision="approved")
        )

    def request_second() -> dict[str, object]:
        role.name = "request"
        return gateway._extensions().request_package_change(
            project_id,
            UUID(str(second["id"])),
            "activate",
            PackageChange(
                expected_sha256=str(second["sha256"]),
                expected_version=str(second["package_version"]),
                expected_revision=int(str(second["revision"])),
                source_run_id=source_run_id,
            ),
        )

    with monkeypatch.context() as patch:
        patch.setattr(ToolGateway, "_require_unblocked_order", staticmethod(after_order_lock))
        patch.setattr(gateway_extension_store, "_lock_project", after_project_lock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            decision = pool.submit(approve_first)
            assert decision_locked_order.wait(timeout=10), "decision did not lock the source work order"
            request = pool.submit(request_second)
            decided = decision.result(timeout=20)
            proposed = request.result(timeout=20)
    assert decided["status"] == "approved"
    assert proposed["status"] == "pending"
    assert client.get(f"{base}/extensions/packages/{first['id']}").json()["state"] == "active"
    assert client.get(f"{base}/extensions/packages/{second['id']}").json()["state"] == "draft"


def test_setup_decision_and_source_run_extension_review_do_not_deadlock(
    setup_task: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, gateway, project, _profiles, tools, claimed, setup_request, _repo = setup_task
    assert isinstance(client, TestClient)
    assert isinstance(gateway, ToolGateway)
    project_id = UUID(str(project))
    base = f"/api/projects/{project_id}"
    proposed_setup = payload(tools.invoke("onefoundry.propose_capability_setup", setup_request, "setup-lock"))
    package = _package(client, base, "identity-mixed-setup-lock")

    decision_locked_order = Event()
    decision_locked_project = Event()
    request_attempted_project = Event()
    request_locked_project = Event()
    role = local()
    original_order_check = ToolGateway._require_unblocked_order
    original_extension_project_lock = gateway_extension_store._lock_project
    original_setup_project_lock = lock_hierarchy_project

    def after_order_lock(connection: Connection, owner: UUID, order_id: UUID) -> None:
        original_order_check(connection, owner, order_id)
        if getattr(role, "name", None) == "decision" and order_id == claimed["root_work_order_id"]:
            decision_locked_order.set()
            if decision_locked_project.is_set():
                assert request_attempted_project.wait(timeout=5), "request did not attempt the project lock"
            else:
                assert request_locked_project.wait(timeout=5), "request did not acquire the project lock"

    def after_extension_project_lock(owner_gateway: ToolGateway, connection: Connection, owner: UUID) -> None:
        if getattr(role, "name", None) == "request":
            request_attempted_project.set()
        original_extension_project_lock(owner_gateway, connection, owner)
        if getattr(role, "name", None) == "request":
            request_locked_project.set()

    def after_setup_project_lock(connection: Connection, owner: UUID) -> None:
        original_setup_project_lock(connection, owner)
        if getattr(role, "name", None) == "decision":
            decision_locked_project.set()

    def decide_setup() -> dict[str, object]:
        role.name = "decision"
        return gateway.decide_approval(
            project_id, UUID(str(proposed_setup["approval_id"])), ApprovalDecision(decision="approved")
        )

    def request_extension_review() -> dict[str, object]:
        role.name = "request"
        return gateway._extensions().request_package_change(
            project_id,
            UUID(str(package["id"])),
            "activate",
            PackageChange(
                expected_sha256=str(package["sha256"]),
                expected_version=str(package["package_version"]),
                expected_revision=int(str(package["revision"])),
                source_run_id=claimed["id"],
            ),
        )

    with monkeypatch.context() as patch:
        patch.setattr(ToolGateway, "_require_unblocked_order", staticmethod(after_order_lock))
        patch.setattr(gateway_extension_store, "_lock_project", after_extension_project_lock)
        patch.setattr(capability_setup, "lock_project", after_setup_project_lock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            decision = pool.submit(decide_setup)
            assert decision_locked_order.wait(timeout=10), "setup decision did not lock the work order"
            request = pool.submit(request_extension_review)
            decided = decision.result(timeout=20)
            extension_review = request.result(timeout=20)
    assert decided["status"] == "approved"
    assert extension_review["status"] == "pending"
    assert client.get(f"{base}/extensions/packages/{package['id']}").json()["state"] == "draft"


def test_profile_decision_and_source_run_extension_review_do_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with profile_env(tmp_path) as (client, gateway):
        project_id, profiles, source_run_id = setup_profile_run(client)
        base = f"/api/projects/{project_id}"
        profile_proposal = propose_profile(
            gateway, project_id, source_run_id, profiles["Chief of Staff"], "profile-lock"
        )
        approval_id = UUID(str(profile_proposal["result"]["approval_id"]))
        package = _package(client, base, "identity-mixed-profile-lock")
        with gateway.engine.connect() as connection:
            source_order_id = connection.scalar(
                text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:r"),
                {"p": project_id, "r": source_run_id},
            )
        assert isinstance(source_order_id, UUID)

        decision_locked_order = Event()
        decision_locked_project = Event()
        request_attempted_project = Event()
        request_locked_project = Event()
        role = local()
        original_order_check = ToolGateway._require_unblocked_order
        original_extension_project_lock = gateway_extension_store._lock_project
        original_profile_project_lock = lock_hierarchy_project

        def after_order_lock(connection: Connection, owner: UUID, order_id: UUID) -> None:
            original_order_check(connection, owner, order_id)
            if getattr(role, "name", None) == "decision" and order_id == source_order_id:
                decision_locked_order.set()
                if decision_locked_project.is_set():
                    assert request_attempted_project.wait(timeout=5), (
                        "request did not attempt the project lock"
                    )
                else:
                    assert request_locked_project.wait(timeout=5), "request did not acquire the project lock"

        def after_extension_project_lock(
            owner_gateway: ToolGateway, connection: Connection, owner: UUID
        ) -> None:
            if getattr(role, "name", None) == "request":
                request_attempted_project.set()
            original_extension_project_lock(owner_gateway, connection, owner)
            if getattr(role, "name", None) == "request":
                request_locked_project.set()

        def after_profile_project_lock(connection: Connection, owner: UUID) -> None:
            original_profile_project_lock(connection, owner)
            if getattr(role, "name", None) == "decision":
                decision_locked_project.set()

        def decide_profile() -> dict[str, object]:
            role.name = "decision"
            return gateway.decide_approval(project_id, approval_id, ApprovalDecision(decision="approved"))

        def request_extension_review() -> dict[str, object]:
            role.name = "request"
            return gateway._extensions().request_package_change(
                project_id,
                UUID(str(package["id"])),
                "activate",
                PackageChange(
                    expected_sha256=str(package["sha256"]),
                    expected_version=str(package["package_version"]),
                    expected_revision=int(str(package["revision"])),
                    source_run_id=source_run_id,
                ),
            )

        with monkeypatch.context() as patch:
            patch.setattr(ToolGateway, "_require_unblocked_order", staticmethod(after_order_lock))
            patch.setattr(gateway_extension_store, "_lock_project", after_extension_project_lock)
            patch.setattr(coordination_profiles, "lock_project", after_profile_project_lock)
            with ThreadPoolExecutor(max_workers=2) as pool:
                decision = pool.submit(decide_profile)
                assert decision_locked_order.wait(timeout=10), "profile decision did not lock the work order"
                request = pool.submit(request_extension_review)
                decided = decision.result(timeout=20)
                extension_review = request.result(timeout=20)
        assert decided["status"] == "approved"
        assert extension_review["status"] == "pending"
        assert "Finance analyst" in {row["name"] for row in client.get(f"{base}/profiles").json()}
        assert client.get(f"{base}/extensions/packages/{package['id']}").json()["state"] == "draft"
