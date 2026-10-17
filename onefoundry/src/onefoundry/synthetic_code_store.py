"""Durable owner path for the bounded offline Engineer, QA, and merge fixture."""

# SQL fragments are fixed module constants; values are always bound.
# ruff: noqa: S608

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import run_audit
from onefoundry.runner_spool import SHA256, RunnerSpool, SpoolInvalid
from onefoundry.synthetic_code_schemas import RunnerReceipt
from onefoundry.tool_gateway import Conflict, NotFound, ToolGateway
from onefoundry.workflow_schemas import SECRET_PATTERN

FIXTURE_ID = "engineer_qa_v1"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^fixture/repair-[0-9a-f-]{36}$")
ARTIFACT_KINDS = {
    "before_log": "before.log",
    "after_log": "after.log",
    "diff": "change.diff",
    "bundle": "repair.bundle",
    "qa_log": "qa.log",
    "merge_log": "merge.log",
}
JOB_COLUMNS = (
    "id, project_id, run_id, stage, status, reason_code, source_job_id, approval_id, "
    "branch, commit, baseline_commit, merged_commit, observed_head_commit, diff_sha256, tests, "
    "created_at, started_at, finished_at"
)


class SyntheticCodeStore:
    def __init__(
        self,
        gateway: ToolGateway,
        spool: RunnerSpool,
        limits: dict[str, int],
        receipt_wait_seconds: int,
    ) -> None:
        self.gateway = gateway
        self.spool = spool
        self.limits = limits
        self.receipt_wait_seconds = receipt_wait_seconds

    def eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            order = (
                connection.execute(
                    text(
                        "SELECT status, profile_id FROM work_orders WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                raise NotFound("work order not found")
            engineer = (
                connection.execute(
                    text(
                        "SELECT id FROM profiles WHERE project_id=:project_id AND id=:id "
                        "AND kind='builtin' AND name='Engineer'"
                    ),
                    {"project_id": project_id, "id": order["profile_id"]},
                )
                .mappings()
                .one_or_none()
            )
            qa = (
                connection.execute(
                    text(
                        "SELECT id FROM profiles WHERE project_id=:project_id "
                        "AND kind='builtin' AND name='QA'"
                    ),
                    {"project_id": project_id},
                )
                .mappings()
                .one_or_none()
            )
            reason = None
            if order["status"] != "ready":
                reason = "work order is not ready"
            elif engineer is None:
                reason = "assign the built-in Engineer profile"
            elif qa is None:
                reason = "built-in QA profile is missing"
            return {
                "eligible": reason is None,
                "reason": reason,
                "fixture_id": FIXTURE_ID,
                "engineer_profile_id": engineer["id"] if engineer else None,
                "qa_profile_id": qa["id"] if qa else None,
            }

    def _manifest(
        self,
        project_id: UUID,
        run_id: UUID,
        job_id: UUID,
        stage: str,
        actor_id: str,
        source: dict[str, Any] | None = None,
        approval_id: UUID | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "job_id": str(job_id),
            "project_id": str(project_id),
            "run_id": str(run_id),
            "fixture_id": FIXTURE_ID,
            "stage": stage,
            "actor_id": actor_id,
            "source_job_id": str(source["id"]) if source else None,
            "approval_id": str(approval_id) if approval_id else None,
            "expected_branch": source["branch"] if source else None,
            "expected_commit": source["commit"] if source else None,
            "expected_baseline_commit": source["baseline_commit"] if source else None,
            "expected_diff_sha256": source["diff_sha256"] if source else None,
            "limits": self.limits,
        }

    def _dispatch(self, project_id: UUID, run_id: UUID, job_id: UUID, manifest: dict[str, Any]) -> None:
        try:
            self.spool.write_job(job_id, manifest)
        except (OSError, SpoolInvalid):
            self._mark_unknown(project_id, run_id, job_id, "manifest_dispatch_uncertain")

    def launch(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        run_id, job_id = uuid4(), uuid4()
        with self.gateway.engine.begin() as connection:
            order = (
                connection.execute(
                    text(
                        "SELECT status, profile_id FROM work_orders WHERE project_id=:project_id "
                        "AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                raise NotFound("work order not found")
            if order["status"] != "ready":
                raise Conflict("work order is not ready")
            eligible = connection.scalar(
                text(
                    "SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:id "
                    "AND kind='builtin' AND name='Engineer'"
                ),
                {"project_id": project_id, "id": order["profile_id"]},
            )
            qa = connection.scalar(
                text("SELECT 1 FROM profiles WHERE project_id=:project_id AND kind='builtin' AND name='QA'"),
                {"project_id": project_id},
            )
            if eligible is None or qa is None:
                raise Conflict("synthetic Engineer and QA profiles required")
            run_audit.insert_run(
                connection,
                project_id,
                run_id,
                order_id,
                order["profile_id"],
                self.gateway.actor_id,
                "synthetic_code",
            )
            connection.execute(
                text("UPDATE work_orders SET status='running' WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": order_id},
            )
            self._insert_job(connection, project_id, run_id, job_id, "engineer")
            self.gateway._event(
                connection,
                project_id,
                order_id,
                run_id,
                "synthetic_code_started",
                {"fixture_id": FIXTURE_ID, "job_id": str(job_id)},
            )
        self._dispatch(
            project_id,
            run_id,
            job_id,
            self._manifest(project_id, run_id, job_id, "engineer", self.gateway.actor_id),
        )
        return self.detail(project_id, run_id)

    def _insert_job(
        self,
        connection: Any,
        project_id: UUID,
        run_id: UUID,
        job_id: UUID,
        stage: str,
        source_job_id: UUID | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO runner_jobs "
                "(id,project_id,actor_id,run_id,stage,status,source_job_id,approval_id) "
                "VALUES (:id,:project_id,:actor_id,:run_id,:stage,'queued',:source_job_id,:approval_id)"
            ),
            {
                "id": job_id,
                "project_id": project_id,
                "actor_id": self.gateway.actor_id,
                "run_id": run_id,
                "stage": stage,
                "source_job_id": source_job_id,
                "approval_id": approval_id,
            },
        )

    def detail(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            run = self.gateway._run_row(connection, project_id, run_id)
            if run["mode"] != "synthetic_code":
                raise Conflict("run is not a synthetic code run")
            jobs = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT {JOB_COLUMNS} FROM runner_jobs WHERE project_id=:project_id "
                        "AND run_id=:run_id ORDER BY created_at,id"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                ).mappings()
            ]
            for job in jobs:
                job["artifacts"] = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT id,kind,path,sha256,byte_size FROM runner_artifacts "
                            "WHERE project_id=:project_id AND job_id=:job_id ORDER BY kind"
                        ),
                        {"project_id": project_id, "job_id": job["id"]},
                    ).mappings()
                ]
            approval_id = connection.scalar(
                text(
                    "SELECT approval_id FROM synthetic_merge_bindings "
                    "WHERE project_id=:project_id AND run_id=:run_id"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            return {"run": run, "jobs": jobs, "synthetic_merge_approval_id": approval_id}

    def _mark_unknown(self, project_id: UUID, run_id: UUID, job_id: UUID, reason: str) -> None:
        with self.gateway.engine.begin() as connection:
            job = (
                connection.execute(
                    text(
                        "SELECT j.status,j.stage,r.work_order_id FROM runner_jobs j "
                        "JOIN runs r ON r.project_id=j.project_id AND r.id=j.run_id "
                        "WHERE j.project_id=:project_id AND j.id=:job_id AND j.run_id=:run_id FOR UPDATE"
                    ),
                    {"project_id": project_id, "run_id": run_id, "job_id": job_id},
                )
                .mappings()
                .one_or_none()
            )
            if job is None or job["status"] != "queued":
                return
            connection.execute(
                text(
                    "UPDATE runner_jobs SET status='unknown',reason_code=:reason "
                    "WHERE project_id=:project_id AND id=:job_id"
                ),
                {"project_id": project_id, "job_id": job_id, "reason": reason},
            )
            if job["stage"] != "merge":
                connection.execute(
                    text(
                        "UPDATE runs SET status='unknown',outcome_reason=:reason,"
                        "finished_at=clock_timestamp() "
                        "WHERE project_id=:project_id AND id=:run_id"
                    ),
                    {"project_id": project_id, "run_id": run_id, "reason": reason},
                )
                self.gateway.settle_work_order(
                    connection, project_id, job["work_order_id"], run_id, "unknown"
                )
            self.gateway._event(
                connection,
                project_id,
                job["work_order_id"],
                run_id,
                "synthetic_code_unknown",
                {"job_id": str(job_id), "reason_code": reason},
            )

    def reconcile(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        detail = self.detail(project_id, run_id)
        queued = [job for job in detail["jobs"] if job["status"] == "queued"]
        if not queued:
            return detail
        job = queued[-1]
        job_id: UUID = job["id"]
        try:
            observed = self.spool.read_receipt(job_id)
            if observed is None:
                claim_age = self.spool.claim_age_seconds(job_id)
                if claim_age is not None and claim_age >= self.receipt_wait_seconds:
                    self._mark_unknown(project_id, run_id, job_id, "claimed_receipt_missing")
                    return self.detail(project_id, run_id)
                if claim_age is None:
                    age = (datetime.now(UTC) - job["created_at"]).total_seconds()
                    if age >= self.receipt_wait_seconds:
                        if self.spool.cancel_unclaimed(job_id):
                            self._mark_unknown(project_id, run_id, job_id, "unclaimed_dispatch_expired")
                            return self.detail(project_id, run_id)
                return detail
            raw, receipt_digest = observed
            receipt = RunnerReceipt.model_validate(raw)
            source_job = next((item for item in detail["jobs"] if item["id"] == job["source_job_id"]), None)
            self._validate_receipt(project_id, run_id, job, receipt, source_job)
            for artifact in receipt.artifacts:
                artifact_bytes = self.spool.read_artifact(
                    job_id, artifact.path, artifact.byte_size, artifact.sha256
                )
                if artifact.kind != "bundle" and SECRET_PATTERN.search(artifact_bytes.decode("utf-8")):
                    raise ValueError("artifact appears to contain a secret")
        except (OSError, SpoolInvalid, ValidationError, ValueError, UnicodeDecodeError):
            self._mark_unknown(project_id, run_id, job_id, "receipt_invalid")
            return self.detail(project_id, run_id)
        next_job: UUID | None = None
        source: dict[str, Any] | None = None
        with self.gateway.engine.begin() as connection:
            locked: str = connection.execute(
                text("SELECT status FROM runner_jobs WHERE project_id=:project_id AND id=:id FOR UPDATE"),
                {"project_id": project_id, "id": job_id},
            ).scalar_one()
            if locked != "queued":
                return self.detail(project_id, run_id)
            connection.execute(
                text(
                    "UPDATE runner_jobs SET status=:status,reason_code=:reason,branch=:branch,"
                    "commit=:commit,baseline_commit=:baseline_commit,merged_commit=:merged_commit,"
                    "observed_head_commit=:observed_head_commit,"
                    "diff_sha256=:diff_sha256,tests=:tests,receipt_sha256=:receipt_sha256,"
                    "started_at=:started_at,finished_at=:finished_at "
                    "WHERE project_id=:project_id AND id=:id"
                ).bindparams(bindparam("tests", type_=JSONB)),
                {
                    "project_id": project_id,
                    "id": job_id,
                    "status": receipt.status,
                    "reason": receipt.reason_code,
                    "branch": receipt.branch,
                    "commit": receipt.commit,
                    "baseline_commit": receipt.baseline_commit,
                    "merged_commit": receipt.merged_commit,
                    "observed_head_commit": receipt.observed_head_commit,
                    "diff_sha256": receipt.diff_sha256,
                    "tests": [test.model_dump() for test in receipt.tests],
                    "receipt_sha256": receipt_digest,
                    "started_at": receipt.started_at,
                    "finished_at": receipt.finished_at,
                },
            )
            for artifact in receipt.artifacts:
                connection.execute(
                    text(
                        "INSERT INTO runner_artifacts "
                        "(id,project_id,actor_id,run_id,job_id,kind,path,sha256,byte_size) "
                        "VALUES (:id,:project_id,:actor_id,:run_id,:job_id,:kind,:path,:sha256,:byte_size)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": f"runner:{FIXTURE_ID}",
                        "run_id": run_id,
                        "job_id": job_id,
                        **artifact.model_dump(),
                    },
                )
            self.gateway._event(
                connection,
                project_id,
                detail["run"]["work_order_id"],
                run_id,
                "synthetic_code_receipt",
                {
                    "job_id": str(job_id),
                    "stage": job["stage"],
                    "status": receipt.status,
                    "reason_code": receipt.reason_code,
                    "tests": [test.model_dump() for test in receipt.tests],
                    "diff_sha256": receipt.diff_sha256,
                    "merged_commit": receipt.merged_commit,
                    "observed_head_commit": receipt.observed_head_commit,
                    "tokens_used": 0,
                    "cost_usd": 0,
                    "receipt_sha256": receipt_digest,
                },
            )
            if receipt.status == "done" and job["stage"] == "engineer":
                next_job = uuid4()
                qa_id = connection.scalar(
                    text(
                        "SELECT id FROM profiles WHERE project_id=:project_id "
                        "AND kind='builtin' AND name='QA'"
                    ),
                    {"project_id": project_id},
                )
                if qa_id is None:
                    raise Conflict("QA profile missing during handoff")
                connection.execute(
                    text("UPDATE runs SET profile_id=:id WHERE project_id=:project_id AND id=:run_id"),
                    {"id": qa_id, "project_id": project_id, "run_id": run_id},
                )
                run_audit.record_handoff(connection, project_id, run_id, qa_id, self.gateway.actor_id)
                connection.execute(
                    text(
                        "UPDATE work_orders SET profile_id=:id WHERE project_id=:project_id AND id=:order_id"
                    ),
                    {"id": qa_id, "project_id": project_id, "order_id": detail["run"]["work_order_id"]},
                )
                self._insert_job(connection, project_id, run_id, next_job, "qa", job_id)
                source = {
                    "id": job_id,
                    "branch": receipt.branch,
                    "commit": receipt.commit,
                    "baseline_commit": receipt.baseline_commit,
                    "diff_sha256": receipt.diff_sha256,
                }
                self.gateway._event(
                    connection,
                    project_id,
                    detail["run"]["work_order_id"],
                    run_id,
                    "synthetic_code_handed_off",
                    {"from": "Engineer", "to": "QA", "job_id": str(next_job)},
                )
            elif job["stage"] != "merge":
                outcome = receipt.status
                connection.execute(
                    text(
                        "UPDATE runs SET status=:status,outcome_reason=:reason,"
                        "finished_at=clock_timestamp() WHERE project_id=:project_id AND id=:run_id"
                    ),
                    {
                        "status": outcome,
                        "reason": receipt.reason_code,
                        "project_id": project_id,
                        "run_id": run_id,
                    },
                )
                self.gateway.settle_work_order(
                    connection, project_id, detail["run"]["work_order_id"], run_id, outcome
                )
        if next_job is not None and source is not None:
            self._dispatch(
                project_id,
                run_id,
                next_job,
                self._manifest(project_id, run_id, next_job, "qa", self.gateway.actor_id, source),
            )
        return self.detail(project_id, run_id)

    @staticmethod
    def _validate_receipt(
        project_id: UUID,
        run_id: UUID,
        job: dict[str, Any],
        receipt: RunnerReceipt,
        source_job: dict[str, Any] | None,
    ) -> None:
        if (
            receipt.project_id != project_id
            or receipt.run_id != run_id
            or receipt.job_id != job["id"]
            or receipt.stage != job["stage"]
            or receipt.finished_at < receipt.started_at
        ):
            raise ValueError("receipt binding mismatch")
        if len({item.kind for item in receipt.artifacts}) != len(receipt.artifacts):
            raise ValueError("duplicate artifact kind")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,159}", receipt.reason_code):
            raise ValueError("invalid receipt reason")
        for artifact in receipt.artifacts:
            if artifact.kind not in ARTIFACT_KINDS or artifact.path != (
                f"artifacts/{job['id']}/{ARTIFACT_KINDS[artifact.kind]}"
            ):
                raise ValueError("artifact kind or path mismatch")
        diff_artifact = next((item for item in receipt.artifacts if item.path.endswith("/change.diff")), None)
        if diff_artifact is not None and receipt.diff_sha256 != diff_artifact.sha256:
            raise ValueError("diff artifact digest mismatch")
        if receipt.status == "done":
            required = {
                "engineer": {"before_log", "after_log", "diff", "bundle"},
                "qa": {"qa_log"},
                "merge": {"merge_log"},
            }[job["stage"]]
            if {item.kind for item in receipt.artifacts} != required:
                raise ValueError("successful receipt missing stage artifacts")
            expected_tests = {
                "engineer": [("before_repair", "failed", 1), ("after_repair", "passed", 0)],
                "qa": [("qa_fixture_test", "passed", 0)],
                "merge": [("merged_fixture_test", "passed", 0)],
            }[job["stage"]]
            if [(test.name, test.status, test.exit_code) for test in receipt.tests] != expected_tests:
                raise ValueError("successful receipt missing stage tests")
            if (
                receipt.branch is None
                or not BRANCH.fullmatch(receipt.branch)
                or receipt.commit is None
                or not COMMIT.fullmatch(receipt.commit)
                or receipt.baseline_commit is None
                or not COMMIT.fullmatch(receipt.baseline_commit)
                or receipt.diff_sha256 is None
                or not SHA256.fullmatch(receipt.diff_sha256)
            ):
                raise ValueError("successful receipt missing exact candidate")
            expected_source_id = job["id"] if job["stage"] == "engineer" else job["source_job_id"]
            if receipt.branch != f"fixture/repair-{expected_source_id}":
                raise ValueError("receipt branch does not match source job")
            if receipt.stage == "merge" and receipt.merged_commit != receipt.commit:
                raise ValueError("merge receipt missing readback")
            if receipt.stage == "merge" and receipt.observed_head_commit != receipt.merged_commit:
                raise ValueError("merge receipt observed head mismatch")
            if receipt.stage != "merge" and receipt.merged_commit is not None:
                raise ValueError("unexpected merged commit")
            if receipt.stage != "merge" and receipt.observed_head_commit is not None:
                raise ValueError("unexpected observed head")
        elif (
            receipt.branch is not None
            or receipt.commit is not None
            or receipt.baseline_commit is not None
            or receipt.merged_commit is not None
            or receipt.diff_sha256 is not None
            or receipt.tests
            or receipt.artifacts
        ):
            raise ValueError("non-success receipt carries candidate")
        if receipt.status == "unknown" and receipt.stage == "merge":
            if receipt.observed_head_commit is not None and not COMMIT.fullmatch(
                receipt.observed_head_commit
            ):
                raise ValueError("invalid observed merge head")
        elif receipt.status != "done" and receipt.observed_head_commit is not None:
            raise ValueError("unexpected observed head")
        if job["stage"] != "engineer":
            if source_job is None or source_job["status"] != "done":
                raise ValueError("source job missing")
            if receipt.status == "done" and any(
                getattr(receipt, key) != source_job[key]
                for key in ("branch", "commit", "baseline_commit", "diff_sha256")
            ):
                raise ValueError("candidate changed between stages")

    def preview(self, project_id: UUID, run_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT a.kind,a.path,a.sha256,a.byte_size,a.job_id FROM runner_artifacts a "
                        "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
                        "WHERE a.project_id=:project_id AND a.run_id=:run_id AND a.id=:id "
                        "AND r.mode='synthetic_code'"
                    ),
                    {"project_id": project_id, "run_id": run_id, "id": artifact_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("artifact not found")
            try:
                data = self.spool.read_artifact(row["job_id"], row["path"], row["byte_size"], row["sha256"])
                preview_text = data.decode("utf-8")
            except (OSError, SpoolInvalid, UnicodeDecodeError) as exc:
                raise Conflict("artifact readback failed") from exc
            if row["kind"] == "bundle":
                raise Conflict("bundle has no text preview")
            return {
                "kind": row["kind"],
                "sha256": row["sha256"],
                "byte_size": row["byte_size"],
                "text": preview_text,
            }

    def request_merge_approval(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        approval_id = uuid4()
        with self.gateway.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id,status,mode FROM runs WHERE project_id=:project_id "
                        "AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["mode"] != "synthetic_code" or run["status"] != "done":
                raise Conflict("QA pass required before merge proposal")
            qa = (
                connection.execute(
                    text(
                        "SELECT status FROM runner_jobs WHERE project_id=:project_id AND run_id=:run_id "
                        "AND stage='qa'"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            source = (
                connection.execute(
                    text(
                        f"SELECT {JOB_COLUMNS} FROM runner_jobs WHERE project_id=:project_id "
                        "AND run_id=:run_id AND stage='engineer'"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if qa is None or qa["status"] != "done" or source is None or source["status"] != "done":
                raise Conflict("verified Engineer and QA receipts required")
            if (
                connection.scalar(
                    text(
                        "SELECT 1 FROM synthetic_merge_bindings WHERE "
                        "project_id=:project_id AND run_id=:run_id"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                is not None
            ):
                raise Conflict("merge proposal already exists")
            proposal = {
                "fixture_id": FIXTURE_ID,
                "project_id": str(project_id),
                "run_id": str(run_id),
                "source_job_id": str(source["id"]),
                "target": "main",
                "baseline_commit": source["baseline_commit"],
                "branch": source["branch"],
                "commit": source["commit"],
                "diff_sha256": source["diff_sha256"],
                "actor_id": self.gateway.actor_id,
            }
            content = json.dumps(proposal, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(content.encode()).hexdigest()
            row = (
                connection.execute(
                    text(
                        "INSERT INTO approval_requests "
                        "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,"
                        "proposal_digest,status) "
                        "VALUES (:id,:project_id,:actor_id,:work_order_id,:run_id,'merge',:target,"
                        ":content,:digest,'pending') "
                        "RETURNING id,project_id,work_order_id,run_id,action_type,target,content,"
                        "proposal_digest,"
                        "status,created_at,decided_at"
                    ),
                    {
                        "id": approval_id,
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "work_order_id": run["work_order_id"],
                        "run_id": run_id,
                        "target": f"{FIXTURE_ID}:main",
                        "content": content,
                        "digest": digest,
                    },
                )
                .mappings()
                .one()
            )
            connection.execute(
                text(
                    "INSERT INTO synthetic_merge_bindings "
                    "(id,project_id,actor_id,run_id,approval_id,source_job_id,branch,commit,"
                    "baseline_commit,diff_sha256) VALUES "
                    "(:id,:project_id,:actor_id,:run_id,:approval_id,:source_job_id,:branch,:commit,"
                    ":baseline_commit,:diff_sha256)"
                ),
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "source_job_id": source["id"],
                    "branch": source["branch"],
                    "commit": source["commit"],
                    "baseline_commit": source["baseline_commit"],
                    "diff_sha256": source["diff_sha256"],
                },
            )
            self.gateway._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "synthetic_merge_requested",
                {"approval_id": str(approval_id), "proposal_digest": digest},
            )
            return {**dict(row), "synthetic_merge": True}

    def approved_merge_job(
        self, connection: Any, project_id: UUID, approval_id: UUID, run_id: UUID
    ) -> tuple[UUID, dict[str, Any]] | None:
        binding = (
            connection.execute(
                text(
                    "SELECT source_job_id,branch,commit,baseline_commit,diff_sha256 "
                    "FROM synthetic_merge_bindings "
                    "WHERE project_id=:project_id AND approval_id=:approval_id AND run_id=:run_id"
                ),
                {"project_id": project_id, "approval_id": approval_id, "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if binding is None:
            return None
        source = (
            connection.execute(
                text(
                    f"SELECT {JOB_COLUMNS} FROM runner_jobs "
                    "WHERE project_id=:project_id AND run_id=:run_id AND id=:id"
                ),
                {"project_id": project_id, "id": binding["source_job_id"], "run_id": run_id},
            )
            .mappings()
            .one()
        )
        if any(binding[key] != source[key] for key in ("branch", "commit", "baseline_commit", "diff_sha256")):
            raise Conflict("merge candidate changed")
        job_id = uuid4()
        self._insert_job(connection, project_id, run_id, job_id, "merge", source["id"], approval_id)
        return job_id, self._manifest(
            project_id, run_id, job_id, "merge", self.gateway.actor_id, dict(source), approval_id
        )
