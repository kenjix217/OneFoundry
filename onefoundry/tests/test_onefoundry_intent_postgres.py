"""Durable event continuation against disposable PostgreSQL, without model calls."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.onefoundry_coordinator import claim_due, run_once
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_onefoundry_context_postgres import client, post, setup, source_and_claim

DRAFT_COUNT = text(
    "SELECT count(*) FROM onefoundry_work_order_subjects WHERE project_id=:p AND step_key='draft'"
)
EXPIRE_LEASE = text(
    "UPDATE coordination_intents SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
)


def gateway(*, order_limit: int = 2, max_attempts: int = 3) -> tuple[ToolGateway, Settings]:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        onefoundry_business_mode="event",
        onefoundry_max_linked_orders=order_limit,
        onefoundry_coordinator_max_attempts=max_attempts,
    )
    return (
        ToolGateway(
            create_engine(settings.database_url),
            settings.onefoundry_coordinator_actor_id,
            live_text_settings=settings,
        ),
        settings,
    )


def qualified(api: TestClient, project_id: str, org_id: str) -> tuple[str, str]:
    source_id, claim_id = source_and_claim(api, project_id, org_id)
    root = f"/api/projects/{project_id}"
    detail = api.get(root + f"/organizations/{org_id}").json()
    claim = next(row for row in detail["claims"] if row["id"] == claim_id)
    reviewed = post(
        api,
        root + f"/claims/{claim_id}/review",
        {
            "decision": "approved",
            "expected_revision": claim["revision"],
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    org = api.get(root + f"/organizations/{org_id}").json()["organization"]
    decided = post(
        api,
        root + f"/organizations/{org_id}/qualification",
        {
            "qualification": "qualified",
            "expected_revision": org["revision"],
        },
    )
    assert decided.status_code == 200, decided.text
    return source_id, claim_id


def intent_row(gw: ToolGateway, project_id: str, org_id: str) -> dict[str, object]:
    with gw.engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id,state,attempts,error_code,work_order_id,workflow_id FROM coordination_intents "
                    "WHERE project_id=:p AND organization_id=:o"
                ),
                {"p": UUID(project_id), "o": UUID(org_id)},
            )
            .mappings()
            .one()
        )
        return dict(row)


def test_qualification_queues_once_and_coordinator_creates_one_ready_order(tmp_path: Path) -> None:
    with client(tmp_path, mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        detail = api.get(f"/api/projects/{project_id}/organizations/{org_id}").json()
        assert detail["intent"]["state"] == "pending"
        assert detail["next_action"]["action_key"] == "pending_draft"
        assert detail["linked_order"] is None
        duplicate = post(
            api,
            f"/api/projects/{project_id}/organizations/{org_id}/qualification",
            {
                "qualification": "qualified",
                "expected_revision": detail["organization"]["revision"],
            },
        )
        assert duplicate.status_code == 200
        gw, settings = gateway()
        assert run_once(gw, settings, UUID(project_id))
        saved = api.get(f"/api/projects/{project_id}/organizations/{org_id}").json()
        assert saved["intent"]["state"] == "done"
        assert saved["linked_order"]["step_key"] == "draft"
        assert saved["linked_order"]["status"] == "ready"
        assert saved["linked_order"]["run_id"] is None
        with gw.engine.connect() as connection:
            count = connection.scalar(
                DRAFT_COUNT,
                {"p": UUID(project_id)},
            )
            assert count == 1
            assert (
                connection.scalar(
                    text("SELECT actor_id FROM work_orders WHERE project_id=:p AND id=:id"),
                    {"p": UUID(project_id), "id": saved["linked_order"]["work_order_id"]},
                )
                == settings.onefoundry_coordinator_actor_id
            )
        assert not run_once(gw, settings, UUID(project_id))
        gw.engine.dispose()


def test_manual_draft_wins_and_two_coordinators_do_not_duplicate(tmp_path: Path) -> None:
    with client(tmp_path, mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        root = f"/api/projects/{project_id}/organizations/{org_id}"
        detail = api.get(root).json()
        sales = next(
            row["id"]
            for row in api.get(f"/api/projects/{project_id}/profiles").json()
            if row["name"] == "Sales"
        )
        manual = post(
            api,
            root + "/draft-orders",
            {
                "profile_id": sales,
                "idempotency_key": str(uuid4()),
                "expected_revision": detail["workflow_revision"],
            },
        )
        assert manual.status_code == 201, manual.text
        gw, settings = gateway()
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert any(executor.map(lambda _: run_once(gw, settings, UUID(project_id)), range(2)))
        assert intent_row(gw, project_id, org_id)["work_order_id"] == UUID(manual.json()["work_order_id"])
        with gw.engine.connect() as connection:
            assert (
                connection.scalar(
                    DRAFT_COUNT,
                    {"p": UUID(project_id)},
                )
                == 1
            )
        gw.engine.dispose()


def test_two_coordinators_claim_one_intent(tmp_path: Path) -> None:
    with client(tmp_path, mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        first, settings = gateway()
        second, _ = gateway()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(lambda gw: run_once(gw, settings, UUID(project_id)), [first, second])
            )
        assert outcomes.count(True) == 1
        with first.engine.connect() as connection:
            assert connection.scalar(DRAFT_COUNT, {"p": UUID(project_id)}) == 1
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p "
                        "AND event_type='onefoundry_order_linked'"
                    ),
                    {"p": UUID(project_id)},
                )
                == 1
            )
        first.engine.dispose()
        second.engine.dispose()


def test_expired_lease_stops_at_attempt_limit(tmp_path: Path) -> None:
    with client(tmp_path, mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        gw, settings = gateway(max_attempts=1)
        claimed = claim_due(gw, settings, UUID(project_id))
        assert claimed is not None
        with gw.engine.begin() as connection:
            connection.execute(EXPIRE_LEASE, {"id": claimed["id"]})
        assert not run_once(gw, settings, UUID(project_id))
        assert intent_row(gw, project_id, org_id)["error_code"] == "attempt_limit"
        with gw.engine.connect() as connection:
            assert connection.scalar(DRAFT_COUNT, {"p": UUID(project_id)}) == 0
        gw.engine.dispose()


def test_expired_lease_recovers_after_crash_and_owner_commit(tmp_path: Path) -> None:
    with client(tmp_path, mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        gw, settings = gateway()
        claimed = claim_due(gw, settings, UUID(project_id))
        assert claimed is not None
        assert not run_once(gw, settings, UUID(project_id))
        with gw.engine.begin() as connection:
            connection.execute(
                EXPIRE_LEASE,
                {"id": claimed["id"]},
            )
        with patch("onefoundry.onefoundry_coordinator._finish", side_effect=RuntimeError("crash")):
            try:
                run_once(gw, settings, UUID(project_id))
            except RuntimeError:
                pass
            else:
                raise AssertionError("simulated crash did not occur")
        with gw.engine.begin() as connection:
            connection.execute(
                EXPIRE_LEASE,
                {"id": claimed["id"]},
            )
        assert run_once(gw, settings, UUID(project_id))
        assert intent_row(gw, project_id, org_id)["state"] == "done"
        with gw.engine.connect() as connection:
            assert (
                connection.scalar(
                    DRAFT_COUNT,
                    {"p": UUID(project_id)},
                )
                == 1
            )
        gw.engine.dispose()


def test_stale_evidence_and_budget_exhaustion_block_without_run(tmp_path: Path) -> None:
    with client(tmp_path / "stale", mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        detail = api.get(f"/api/projects/{project_id}/organizations/{org_id}").json()
        changed = post(
            api,
            f"/api/projects/{project_id}/organizations/{org_id}/sources",
            {
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/new",
                "excerpt": "New and unrelated source text.",
                "observed_at": "2026-09-29T12:00:00Z",
                "expected_workflow_revision": detail["workflow_revision"],
            },
        )
        assert changed.status_code == 201, changed.text
        gw, settings = gateway()
        assert run_once(gw, settings, UUID(project_id))
        assert intent_row(gw, project_id, org_id)["state"] == "blocked"
        gw.engine.dispose()
    with client(tmp_path / "budget", mode="event", onefoundry_max_linked_orders=1) as api:
        project_id, org_id, researcher, _ = setup(api)
        source_and_claim(api, project_id, org_id)
        detail = api.get(f"/api/projects/{project_id}/organizations/{org_id}").json()
        research = post(
            api,
            f"/api/projects/{project_id}/organizations/{org_id}/research-orders",
            {
                "profile_id": researcher,
                "idempotency_key": str(uuid4()),
                "expected_revision": detail["workflow_revision"],
            },
        )
        assert research.status_code == 201, research.text
        claim = api.get(f"/api/projects/{project_id}/organizations/{org_id}").json()["claims"][0]
        assert (
            post(
                api,
                f"/api/projects/{project_id}/claims/{claim['id']}/review",
                {
                    "decision": "approved",
                    "expected_revision": claim["revision"],
                },
            ).status_code
            == 200
        )
        org = api.get(f"/api/projects/{project_id}/organizations/{org_id}").json()["organization"]
        assert (
            post(
                api,
                f"/api/projects/{project_id}/organizations/{org_id}/qualification",
                {
                    "qualification": "qualified",
                    "expected_revision": org["revision"],
                },
            ).status_code
            == 200
        )
        gw, settings = gateway(order_limit=1)
        assert run_once(gw, settings, UUID(project_id))
        assert intent_row(gw, project_id, org_id)["error_code"] == "budget_exceeded"
        with gw.engine.connect() as connection:
            assert (
                connection.scalar(
                    DRAFT_COUNT,
                    {"p": UUID(project_id)},
                )
                == 0
            )
        gw.engine.dispose()


def test_unknown_owner_readback_does_not_retry(tmp_path: Path) -> None:
    with client(tmp_path, mode="event") as api:
        project_id, org_id, _, _ = setup(api)
        qualified(api, project_id, org_id)
        gw, settings = gateway()
        with patch("onefoundry.onefoundry_coordinator._existing_order", side_effect=[None, None]):
            assert run_once(gw, settings, UUID(project_id))
        assert intent_row(gw, project_id, org_id)["state"] == "unknown"
        assert not run_once(gw, settings, UUID(project_id))
        gw.engine.dispose()
