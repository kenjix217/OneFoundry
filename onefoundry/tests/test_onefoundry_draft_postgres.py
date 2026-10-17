"""Strict candidate admission and cited draft revisions with synthetic saved run output."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry import run_audit, text_role_store
from tests.test_onefoundry_budget_postgres import _gateway
from tests.test_onefoundry_context_postgres import client, post, setup, source_and_claim


def _saved_research_output(project_id: str, order_id: str, profile_id: str, body: str) -> str:
    gateway = _gateway()
    run_id = uuid4()
    with gateway.engine.begin() as connection:
        connection.execute(
            text("SELECT id FROM work_orders WHERE project_id=:p AND id=:id FOR UPDATE"),
            {"p": UUID(project_id), "id": UUID(order_id)},
        ).one()
        run_audit.insert_run(
            connection,
            UUID(project_id),
            run_id,
            UUID(order_id),
            UUID(profile_id),
            "operator",
            "synthetic_text",
        )
        connection.execute(
            text(
                "UPDATE runs SET status='done',finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
            ),
            {"p": UUID(project_id), "id": run_id},
        )
        connection.execute(
            text("UPDATE work_orders SET status='done' WHERE project_id=:p AND id=:id"),
            {"p": UUID(project_id), "id": UUID(order_id)},
        )
        text_role_store.insert_output(
            connection,
            output_id=uuid4(),
            project_id=UUID(project_id),
            run_id=run_id,
            profile_id=UUID(profile_id),
            actor_id="profile:" + profile_id,
            role="Researcher",
            configured_model=None,
            body=body,
            digest=hashlib.sha256(body.encode()).hexdigest(),
        )
    gateway.engine.dispose()
    return str(run_id)


def test_candidate_import_rejects_malformed_and_unsupported_output(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        project_id, org_id, researcher, _ = setup(api)
        source_id, _ = source_and_claim(api, project_id, org_id)
        root = f"/api/projects/{project_id}/organizations/{org_id}"
        revision = api.get(root).json()["workflow_revision"]
        order = post(
            api,
            root + "/research-orders",
            {
                "profile_id": researcher,
                "idempotency_key": str(uuid4()),
                "expected_revision": revision,
            },
        ).json()
        run_id = _saved_research_output(project_id, order["work_order_id"], researcher, "analysis only")
        assert post(api, root + f"/research-candidates/from-run/{run_id}").status_code == 409
        assert api.get(root).json()["claims"]
        unsupported = json.dumps(
            [
                {
                    "source_id": source_id,
                    "claim_text": "Unsupported model hypothesis",
                    "evidence_locator": "About page",
                    "observed_at": next(
                        s["observed_at"] for s in api.get(root).json()["sources"] if s["id"] == source_id
                    ),
                }
            ]
        )
        unsupported_run = _saved_research_output(project_id, order["work_order_id"], researcher, unsupported)
        assert post(api, root + f"/research-candidates/from-run/{unsupported_run}").status_code == 409
        gateway = _gateway()
        with gateway.engine.begin() as connection:
            connection.execute(
                text("UPDATE runs SET status='unknown' WHERE project_id=:p AND id=:run"),
                {"p": UUID(project_id), "run": UUID(unsupported_run)},
            )
        assert post(api, root + f"/research-candidates/from-run/{unsupported_run}").status_code == 409
        gateway.engine.dispose()
        wrong = str(uuid4())
        assert post(api, root + f"/research-candidates/from-run/{wrong}").status_code == 404
        # The model's prose remains visible as a run output, never an approved claim.
        gateway = _gateway()
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT text FROM text_outputs WHERE project_id=:p AND run_id=:run"),
                    {"p": UUID(project_id), "run": UUID(run_id)},
                )
                == "analysis only"
            )
        gateway.engine.dispose()
        assert source_id


def test_supported_candidate_and_cited_draft_revision(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        project_id, org_id, researcher, gtm = setup(api)
        source_id, _ = source_and_claim(api, project_id, org_id)
        root = f"/api/projects/{project_id}/organizations/{org_id}"
        project_root = f"/api/projects/{project_id}"
        source = next(s for s in api.get(root).json()["sources"] if s["id"] == source_id)
        observed_at = source["observed_at"]
        revision = api.get(root).json()["workflow_revision"]
        research = post(
            api,
            root + "/research-orders",
            {
                "profile_id": researcher,
                "idempotency_key": str(uuid4()),
                "expected_revision": revision,
            },
        )
        assert research.status_code == 201, research.text
        output = json.dumps(
            [
                {
                    "source_id": source_id,
                    "claim_text": "Fictional Cedar District serves four schools.",
                    "evidence_locator": "About page",
                    "observed_at": observed_at,
                }
            ]
        )
        run_id = _saved_research_output(project_id, research.json()["work_order_id"], researcher, output)
        imported = post(api, root + f"/research-candidates/from-run/{run_id}")
        assert imported.status_code == 201, imported.text
        claim_id = imported.json()[0]["id"]
        assert imported.json()[0]["review_state"] == "candidate"
        assert post(api, root + f"/research-candidates/from-run/{run_id}").status_code == 409
        statement = post(
            api,
            project_root + "/company-statements",
            {"body": "OneFoundry drafts cited answers for staff review."},
        ).json()
        assert (
            post(
                api, project_root + f"/company-statements/{statement['id']}/approve", {"expected_version": 1}
            ).status_code
            == 200
        )
        assert (
            post(
                api,
                project_root + f"/claims/{claim_id}/review",
                {
                    "expected_revision": 1,
                    "decision": "approved",
                },
            ).status_code
            == 200
        )
        detail = api.get(root).json()
        assert (
            post(
                api,
                root + "/qualification",
                {
                    "expected_revision": detail["organization"]["revision"],
                    "qualification": "qualified",
                },
            ).status_code
            == 200
        )
        draft_order = post(
            api,
            root + "/draft-orders",
            {
                "profile_id": gtm,
                "idempotency_key": str(uuid4()),
                "expected_revision": api.get(root).json()["workflow_revision"],
            },
        )
        assert draft_order.status_code == 201, draft_order.text
        packet_id = draft_order.json()["context_packet_id"]
        segments = [
            {"kind": "plain", "text": "Hello district team,"},
            {"kind": "district_fact", "text": "Your district serves four schools.", "claim_id": claim_id},
            {
                "kind": "company_fact",
                "text": "OneFoundry drafts cited answers for review.",
                "statement_id": statement["id"],
                "statement_version": 1,
            },
        ]
        saved = post(
            api,
            root + "/drafts",
            {"context_packet_id": packet_id, "segments": segments, "expected_revision": 0},
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["revision"] == 1
        assert (
            post(
                api,
                root + "/drafts",
                {"context_packet_id": packet_id, "segments": segments, "expected_revision": 0},
            ).status_code
            == 409
        )
        assert api.get(root).json()["drafts"][0]["id"] == saved.json()["id"]
        assert (
            post(
                api, project_root + f"/company-statements/{statement['id']}/retire", {"expected_version": 1}
            ).status_code
            == 200
        )
        assert (
            post(
                api,
                root + "/drafts",
                {"context_packet_id": packet_id, "segments": [segments[2]], "expected_revision": 1},
            ).status_code
            == 409
        )
        current = api.get(root).json()
        changed = post(
            api,
            root + "/sources",
            {
                "source_kind": "public_excerpt",
                "official_url": "https://example.org/about",
                "excerpt": "Fictional Cedar District now serves five schools.",
                "observed_at": datetime.now(UTC).isoformat(),
                "supersedes_source_id": source_id,
                "expected_workflow_revision": current["workflow_revision"],
            },
        )
        assert changed.status_code == 201, changed.text
        assert (
            post(
                api,
                root + "/drafts",
                {
                    "context_packet_id": packet_id,
                    "segments": [segments[1]],
                    "expected_revision": 1,
                },
            ).status_code
            == 409
        )
