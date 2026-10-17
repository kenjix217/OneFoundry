"""Inbox and outcome projections use saved project owners, not event counts."""

import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from onefoundry.settings import Settings
from tests.test_onefoundry_context_postgres import client, post, setup, source_and_claim


def test_inbox_actions_outcomes_and_project_scope(tmp_path: Path) -> None:
    with client(tmp_path, mode="inbox") as api:
        project_id, org_id, _, _ = setup(api)
        root = f"/api/projects/{project_id}"
        empty = api.get(root + "/onefoundry/outcomes")
        assert empty.status_code == 200, empty.text
        assert empty.json()["reviewed_research"] == 0
        assert empty.json()["qualified_districts"] == 0
        assert empty.json()["saved_drafts"] == 0
        assert empty.json()["sent"] == 0
        assert empty.json()["observed_cost_usd"] is None
        initial = api.get(root + "/onefoundry/inbox").json()
        assert len(initial["actionable"]) == 1
        assert initial["actionable"][0]["action"]["action_key"] == "add_source"
        assert initial["actionable"][0]["subject_ref"]["id"] == org_id

        _, claim_id = source_and_claim(api, project_id, org_id)
        candidate = api.get(root + "/onefoundry/inbox").json()["actionable"][0]
        assert candidate["action"]["action_key"] == "review_claims"
        assert (
            candidate["action"]["workflow_revision"] > initial["actionable"][0]["action"]["workflow_revision"]
        )
        claim = api.get(root + f"/organizations/{org_id}").json()["claims"][0]
        decision = post(
            api,
            root + f"/claims/{claim_id}/review",
            {"expected_revision": claim["revision"], "decision": "approved"},
        )
        assert decision.status_code == 200, decision.text
        approved = api.get(root + "/onefoundry/inbox").json()["actionable"][0]
        assert approved["action"]["action_key"] == "qualify"
        assert approved["evidence_state"] == "approved"
        detail = api.get(root + f"/organizations/{org_id}").json()
        assert approved["action"] == detail["next_action"]
        qualified = post(
            api,
            root + f"/organizations/{org_id}/qualification",
            {"expected_revision": detail["organization"]["revision"], "qualification": "qualified"},
        )
        assert qualified.status_code == 200, qualified.text
        assert api.get(root + "/onefoundry/outcomes").json()["qualified_districts"] == 1

        other_project, other_org, _, _ = setup(api)
        other = api.get(f"/api/projects/{other_project}/onefoundry/inbox").json()
        assert [x["subject_ref"]["id"] for x in other["actionable"]] == [other_org]
        assert api.get(f"/api/projects/{uuid4()}/onefoundry/inbox").status_code == 404
        assert (
            api.get(f"/api/projects/{other_project}/onefoundry/outcomes").json()["qualified_districts"] == 0
        )


def test_rejected_claim_is_not_approved_evidence(tmp_path: Path) -> None:
    with client(tmp_path, mode="inbox") as api:
        project_id, org_id, _, _ = setup(api)
        root = f"/api/projects/{project_id}"
        _, claim_id = source_and_claim(api, project_id, org_id)
        claim = api.get(root + f"/organizations/{org_id}").json()["claims"][0]
        decision = post(
            api,
            root + f"/claims/{claim_id}/review",
            {"expected_revision": claim["revision"], "decision": "rejected"},
        )
        assert decision.status_code == 200, decision.text
        item = api.get(root + "/onefoundry/inbox").json()["actionable"][0]
        assert item["evidence_state"] == "none"
        assert item["action"]["action_key"] == "research"
        assert api.get(root + "/onefoundry/outcomes").json()["reviewed_research"] == 0


def test_mixed_owner_states_duplicate_events_and_unknown_usage(tmp_path: Path) -> None:
    with client(tmp_path, mode="inbox") as api:
        project_id, waiting_org, _, _ = setup(api)
        root = f"/api/projects/{project_id}"
        blocked_org = post(
            api, root + "/organizations", {"display_name": "Fictional Blocked District"}
        ).json()["id"]
        unknown_org = post(
            api, root + "/organizations", {"display_name": "Fictional Unknown District"}
        ).json()["id"]
        settings = Settings(
            db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
            db_password=os.environ["APP_DB_PASSWORD"],
        )
        engine = create_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE onefoundry_workflows SET status='research_requested',revision=revision+1 "
                    "WHERE project_id=:p AND organization_id=:o"
                ),
                {"p": UUID(project_id), "o": UUID(waiting_org)},
            )
            connection.execute(
                text(
                    "UPDATE onefoundry_workflows SET status='blocked',blocked_reason='usage_unknown',"
                    "revision=revision+1 WHERE project_id=:p AND organization_id=:o"
                ),
                {"p": UUID(project_id), "o": UUID(blocked_org)},
            )
            connection.execute(
                text(
                    "UPDATE onefoundry_workflows SET status='unknown',revision=revision+1 "
                    "WHERE project_id=:p AND organization_id=:o"
                ),
                {"p": UUID(project_id), "o": UUID(unknown_org)},
            )
            workflow_id = connection.scalar(
                text("SELECT id FROM onefoundry_workflows WHERE project_id=:p AND organization_id=:o"),
                {"p": UUID(project_id), "o": UUID(waiting_org)},
            )
            connection.execute(
                text(
                    "INSERT INTO onefoundry_budget_reservations "
                    "(id,project_id,actor_id,workflow_id,operation_key,kind,state) "
                    "VALUES (:id,:p,'operator',:w,'inbox-test','model_attempt','unknown')"
                ),
                {"id": uuid4(), "p": UUID(project_id), "w": workflow_id},
            )
            for _ in range(2):
                connection.execute(
                    text(
                        "INSERT INTO events (id,project_id,actor_id,event_type,payload) "
                        "VALUES (:id,:p,'operator','onefoundry_claim_reviewed','{}'::jsonb)"
                    ),
                    {"id": uuid4(), "p": UUID(project_id)},
                )
        grouped = api.get(root + "/onefoundry/inbox").json()
        assert [x["subject_ref"]["id"] for x in grouped["waiting"]] == [waiting_org]
        assert [x["subject_ref"]["id"] for x in grouped["blocked"]] == [blocked_org]
        assert [x["subject_ref"]["id"] for x in grouped["unknown"]] == [unknown_org]
        assert grouped["blocked"][0]["blocked_reason"] == "usage_unknown"
        counts = api.get(root + "/onefoundry/outcomes").json()
        assert counts["reviewed_research"] == 0
        assert counts["observed_tokens"] is None
        assert counts["observed_cost_usd"] is None
        engine.dispose()


def test_legacy_project_without_business_rows(tmp_path: Path) -> None:
    with client(tmp_path, mode="inbox") as api:
        project = post(api, "/api/projects", {"name": "Legacy empty"})
        assert project.status_code == 201, project.text
        root = f"/api/projects/{project.json()['id']}/onefoundry"
        assert api.get(root + "/inbox").json() == {
            "actionable": [],
            "waiting": [],
            "blocked": [],
            "unknown": [],
        }
        assert api.get(root + "/outcomes").json()["saved_drafts"] == 0
