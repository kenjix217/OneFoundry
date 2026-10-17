"""Durable dispatch and receipt reconciliation for delegated fixture coding."""

# SQL fragments below are fixed module constants; all request values are bound.
# ruff: noqa: S608

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import run_audit
from onefoundry.action_trace_contract import SavedActionTrace
from onefoundry.external_code_schemas import AdapterId, AttemptAdapterId, ExternalCodeReceipt, TargetId
from onefoundry.github_snapshot_codex_contract import expected_diff
from onefoundry.input_file_runner import prepare_runner_context, record_runner_context
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from onefoundry.source_store import verify_local
from onefoundry.workflow_schemas import SECRET_PATTERN

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

FIXTURE_ID = "engineer_qa_v1"
ADAPTERS: tuple[AdapterId, ...] = ("codex_cli", "claude_code")
BRANCH = re.compile(r"^fixture/repair-[0-9a-f-]{36}$")
PROBE_BRANCH = re.compile(r"^onefoundry/probe-[0-9a-f-]{36}$")
SNAPSHOT_BRANCH = re.compile(r"^fixture/snapshot-[0-9a-f-]{36}$")
ATTEMPT_COLUMNS = (
    "id,project_id,run_id,adapter_id,fixture_id,fixture_id AS target_id,"
    "source_id,source_root_id,source_relative_path,source_branch,source_revision,"
    "status,reason_code,provider_session_id,"
    "baseline_commit,branch,commit,diff_sha256,tests,tokens_used,cost_usd,receipt_sha256,"
    "created_at,started_at,finished_at,request_sha256,snapshot_attempt_id,idempotency_key,"
    "intent_sha256,snapshot_source_id,snapshot_account_id,snapshot_owner,snapshot_repo,"
    "snapshot_branch,snapshot_revision,snapshot_tree,"
    "snapshot_path,snapshot_sha256,snapshot_byte_size,snapshot_request_sha256,"
    "snapshot_receipt_sha256"
)


class ExecutionGateway:
    def __init__(
        self,
        gateway: "ToolGateway",
        spool: RunnerSpool,
        limits: dict[str, int],
        receipt_wait_seconds: int,
        enabled: bool,
        capability_max_age_seconds: int,
    ) -> None:
        self.gateway = gateway
        self.spool = spool
        self.limits = {key: limits[key] for key in ("wall_seconds", "max_log_bytes", "max_diff_bytes")}
        self.receipt_wait_seconds = receipt_wait_seconds
        self.enabled = enabled
        self.capability_max_age_seconds = capability_max_age_seconds

    def launch_snapshot_codex(
        self, project_id: UUID, order_id: UUID, snapshot_attempt_id: UUID, idempotency_key: UUID
    ) -> dict[str, Any]:
        from onefoundry.github_snapshot_codex_gateway import launch

        return launch(self, project_id, order_id, snapshot_attempt_id, idempotency_key)

    def launch_repository_code(self, project_id: UUID, order_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.repository_code_contract import RepositoryCodeLaunch
        from onefoundry.repository_code_gateway import launch

        return launch(self, project_id, order_id, RepositoryCodeLaunch.model_validate(data))

    def snapshot_codex_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        from onefoundry.github_snapshot_codex_gateway import eligibility

        return eligibility(self, project_id, order_id)

    def _capabilities(self) -> dict[str, str | None]:
        """A fresh runner report is required even when dispatch is enabled."""
        unavailable: dict[str, str | None] = {
            adapter: "Coding runner capability is unavailable." for adapter in ADAPTERS
        }
        try:
            raw = self.spool._read_regular(self.spool.root / "capabilities.json", 4096)
            value = json.loads(raw)
            observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - observed).total_seconds()
            if (
                value["schema_version"] != 1
                or observed.tzinfo is None
                or not 0 <= age <= self.capability_max_age_seconds
                or value.get("mode") != "trusted_local_cli_v1"
                or value["isolated"] is not False
                or value["auth_ready"] is not True
                or not isinstance(value["adapters"], list)
            ):
                return unavailable
            reported: dict[str, str | None] = {}
            for item in value["adapters"]:
                adapter = item["adapter_id"]
                if adapter not in ADAPTERS or adapter in reported:
                    return unavailable
                reason = item["reason"]
                if item["available"] is True and reason is None:
                    reported[adapter] = None
                elif item["available"] is False and isinstance(reason, str) and 0 < len(reason) <= 160:
                    reported[adapter] = reason
                else:
                    return unavailable
            return {
                adapter: reported.get(adapter, "Coding adapter was not reported by the runner.")
                for adapter in ADAPTERS
            }
        except (OSError, ValueError, TypeError, KeyError, AttributeError, SpoolInvalid):
            return unavailable

    def _offline_probe_capability(self) -> str | None:
        """Only a fresh, separate host supervisor self-test admits offline probes."""
        settings = self.gateway.live_text_settings
        if settings is None or not settings.offline_source_probe_enabled:
            return "Offline source probe is not enabled."
        try:
            raw = self.spool._read_regular(self.spool.root / "probe_capabilities.json", 4096)
            value = json.loads(raw)
            observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - observed).total_seconds()
            if (
                value["schema_version"] != 1
                or observed.tzinfo is None
                or not 0 <= age <= settings.offline_source_probe_capability_max_age_seconds
                or value["isolated"] is not True
                or value["source_root_ready"] is not True
                or value["available"] is not True
                or value["reason"] is not None
            ):
                return "Offline source probe supervisor is not ready."
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, SpoolInvalid):
            return "Offline source probe supervisor is unavailable."

    def _eligible_order(self, project_id: UUID, order_id: UUID, offline: bool) -> str | None:
        with self.gateway.engine.connect() as connection:
            order = (
                connection.execute(
                    text(
                        "SELECT status,profile_id,title,instructions FROM work_orders "
                        "WHERE project_id=:project_id AND id=:id"
                    ),
                    {"project_id": project_id, "id": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                from onefoundry.gateway_errors import NotFound

                raise NotFound("work order not found")
            engineer = connection.scalar(
                text(
                    "SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:id "
                    "AND kind='builtin' AND name='Engineer'"
                ),
                {"project_id": project_id, "id": order["profile_id"]},
            )
        return self._eligibility_reason(order, engineer is not None, offline)

    def eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        reason = self._eligible_order(project_id, order_id, False)
        capability = self._capabilities() if reason is None else {adapter: reason for adapter in ADAPTERS}
        if reason is None and all(capability[adapter] is not None for adapter in ADAPTERS):
            reason = "No coding adapter is currently ready."
        return {
            "eligible": reason is None,
            "reason": reason,
            "adapters": [
                {
                    "adapter_id": adapter,
                    "available": capability[adapter] is None,
                    "reason": capability[adapter],
                }
                for adapter in ADAPTERS
            ],
        }

    def offline_probe_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        reason = self._eligible_order(project_id, order_id, True)
        if reason is None:
            reason = self._offline_probe_capability()
        return {"eligible": reason is None, "reason": reason}

    def _eligibility_reason(self, order: Any, is_engineer: bool, offline: bool = False) -> str | None:
        if offline and not (
            self.gateway.live_text_settings and self.gateway.live_text_settings.offline_source_probe_enabled
        ):
            return "Offline source probe is not enabled."
        if not offline and not self.enabled:
            return "External coding runner and authentication are not enabled."
        if order["status"] != "ready":
            return "work order is not ready"
        if not is_engineer:
            return "assign the built-in Engineer profile"
        if SECRET_PATTERN.search(order["title"]) or SECRET_PATTERN.search(order["instructions"]):
            return "work order appears to contain a secret"
        if len(order["title"].encode()) + len(order["instructions"].encode()) > 6000:
            return "work order exceeds the bounded runner manifest"
        return None

    def _manifest(
        self,
        project_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        adapter_id: AttemptAdapterId,
        title: str,
        instructions: str,
        target_id: TargetId,
        source: dict[str, str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1 if source is None else 2,
            "job_id": str(attempt_id),
            "project_id": str(project_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "adapter_id": adapter_id,
            "stage": "engineer",
            "actor_id": self.gateway.actor_id,
            "task_title": title,
            "task_instructions": instructions,
            "limits": self.limits,
        }
        if source is None:
            payload["fixture_id"] = FIXTURE_ID
        else:
            payload["target_id"] = target_id
            payload["source"] = source
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["request_sha256"] = hashlib.sha256(canonical).hexdigest()
        if len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()) > 8192:
            from onefoundry.gateway_errors import Conflict

            raise Conflict("work order exceeds the bounded runner manifest")
        return payload

    def launch(
        self,
        project_id: UUID,
        order_id: UUID,
        adapter_id: AttemptAdapterId,
        target_id: TargetId = "engineer_qa_v1",
        source_id: UUID | None = None,
    ) -> dict[str, Any]:
        from onefoundry.gateway_errors import Conflict, NotFound, Unavailable

        offline = target_id == "local_source_probe_v1"
        if offline:
            if adapter_id != "offline_probe" or source_id is None:
                raise Conflict("offline source probe requires source_id and offline_probe adapter")
            capability_reason = self._offline_probe_capability()
            if capability_reason is not None:
                raise Unavailable(capability_reason)
        else:
            if source_id is not None or adapter_id not in ADAPTERS:
                raise Conflict("external coding accepts only live fixture adapters")
            if not self.enabled:
                raise Unavailable("external coding runner and authentication are not enabled")
        run_id, attempt_id = uuid4(), uuid4()
        with self.gateway.engine.begin() as connection:
            order = (
                connection.execute(
                    text(
                        "SELECT status,profile_id,title,instructions FROM work_orders "
                        "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                raise NotFound("work order not found")
            engineer = connection.scalar(
                text(
                    "SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:id "
                    "AND kind='builtin' AND name='Engineer'"
                ),
                {"project_id": project_id, "id": order["profile_id"]},
            )
            reason = self._eligibility_reason(order, engineer is not None, offline)
            if reason is not None:
                raise Conflict(reason)
            if offline:
                capability_reason = self._offline_probe_capability()
                if capability_reason is not None:
                    raise Unavailable(capability_reason)
            else:
                capability = self._capabilities()
                if capability[adapter_id] is not None:
                    raise Unavailable(capability[adapter_id])
            source: dict[str, str] | None = None
            if source_id is not None:
                row = (
                    connection.execute(
                        text(
                            "SELECT kind,root_id,relative_path,branch,revision FROM project_sources "
                            "WHERE project_id=:project_id AND id=:source_id FOR KEY SHARE"
                        ),
                        {"project_id": project_id, "source_id": source_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise NotFound("selected source not found")
                if row["kind"] != "local" or row["root_id"] != "workspace":
                    raise Conflict("selected source must be a local repository")
                if self.gateway.live_text_settings is None:
                    raise Unavailable("local source configuration unavailable")
                branch, revision = verify_local(self.gateway.live_text_settings, row["relative_path"])
                if (branch, revision) != (row["branch"], row["revision"]):
                    raise Conflict("selected source branch or revision changed; select the repository again")
                source = {
                    "source_id": str(source_id),
                    "root_id": row["root_id"],
                    "relative_path": row["relative_path"],
                    "branch": row["branch"],
                    "revision": row["revision"],
                }
            settings = self.gateway.live_text_settings
            if settings is None:
                raise Unavailable("input-file configuration unavailable")
            task_instructions, selected_files = prepare_runner_context(
                connection, settings, project_id, order_id, order["instructions"]
            )
            manifest = self._manifest(
                project_id,
                run_id,
                attempt_id,
                adapter_id,
                order["title"],
                task_instructions,
                target_id,
                source,
            )
            run_audit.insert_run(
                connection,
                project_id,
                run_id,
                order_id,
                order["profile_id"],
                self.gateway.actor_id,
                "external_code",
            )
            record_runner_context(connection, project_id, run_id, self.gateway.actor_id, selected_files)
            connection.execute(
                text("UPDATE work_orders SET status='running' WHERE project_id=:project_id AND id=:id"),
                {"project_id": project_id, "id": order_id},
            )
            connection.execute(
                text(
                    "INSERT INTO external_code_attempts "
                    "(id,project_id,run_id,actor_id,adapter_id,fixture_id,request_sha256,status,"
                    "source_id,source_root_id,source_relative_path,source_branch,source_revision) "
                    "VALUES (:id,:project_id,:run_id,:actor_id,:adapter_id,:fixture_id,"
                    ":request_sha256,'queued',:source_id,:source_root_id,:source_relative_path,"
                    ":source_branch,:source_revision)"
                ),
                {
                    "id": attempt_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "actor_id": self.gateway.actor_id,
                    "adapter_id": adapter_id,
                    "fixture_id": target_id,
                    "request_sha256": manifest["request_sha256"],
                    "source_id": source_id,
                    "source_root_id": source["root_id"] if source else None,
                    "source_relative_path": source["relative_path"] if source else None,
                    "source_branch": source["branch"] if source else None,
                    "source_revision": source["revision"] if source else None,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                order_id,
                run_id,
                "external_code_queued",
                {
                    "attempt_id": str(attempt_id),
                    "adapter_id": adapter_id,
                    **(
                        {"fixture_id": FIXTURE_ID}
                        if source_id is None
                        else {"target_id": target_id, "source_id": str(source_id)}
                    ),
                    "request_sha256": manifest["request_sha256"],
                },
            )
        try:
            self.spool.write_job(
                attempt_id,
                manifest,
                group_readable=target_id == FIXTURE_ID and not offline,
            )
        except (OSError, SpoolInvalid):
            self._mark_unknown(project_id, run_id, attempt_id, "manifest_dispatch_uncertain")
        return self.detail(project_id, run_id)

    def detail(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        from onefoundry.gateway_errors import Conflict

        with self.gateway.engine.connect() as connection:
            run = self.gateway._run_row(connection, project_id, run_id)
            if run["mode"] != "external_code":
                raise Conflict("run is not an external coding run")
            attempts = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT {ATTEMPT_COLUMNS} FROM external_code_attempts "
                        "WHERE project_id=:project_id AND run_id=:run_id ORDER BY created_at,id"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                ).mappings()
            ]
            for attempt in attempts:
                attempt["artifacts"] = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT id,kind,path,sha256,byte_size FROM external_code_artifacts "
                            "WHERE project_id=:project_id AND attempt_id=:attempt_id ORDER BY kind"
                        ),
                        {"project_id": project_id, "attempt_id": attempt["id"]},
                    ).mappings()
                ]
            return {"run": run, "attempts": attempts}

    def progress(self, project_id: UUID, run_id: UUID, attempt_id: UUID, after: int) -> dict[str, Any]:
        """Return a bounded live prefix only for the exact project/run/attempt."""
        from onefoundry.gateway_errors import NotFound, Unavailable

        if type(after) is not int or not 0 <= after <= 24:
            raise ValueError("progress cursor invalid")
        with self.gateway.engine.connect() as connection:
            run = self.gateway._run_row(connection, project_id, run_id)
            if run["mode"] != "external_code":
                raise NotFound("coding attempt not found")
            attempt = (
                connection.execute(
                    text(
                        "SELECT adapter_id,status,request_sha256 FROM external_code_attempts "
                        "WHERE project_id=:project_id AND run_id=:run_id AND id=:attempt_id"
                    ),
                    {"project_id": project_id, "run_id": run_id, "attempt_id": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
        if attempt is None or attempt["adapter_id"] not in ADAPTERS:
            raise NotFound("coding attempt not found")
        try:
            snapshot = self.spool.read_live_progress(
                project_id, run_id, attempt_id, attempt["request_sha256"]
            )
        except (OSError, SpoolInvalid):
            raise Unavailable("coding progress unavailable") from None
        return {
            "project_id": project_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "coverage": "partial",
            "latest_sequence": snapshot.latest_sequence,
            "terminal": attempt["status"] in ("done", "failed", "unknown"),
            "items": [
                {"sequence": record.sequence, **vars(record.progress)}
                for record in snapshot.records
                if record.sequence > after
            ],
        }

    def _mark_unknown(
        self,
        project_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
        reason: str,
        launch_evidence: dict[str, Any] | None = None,
    ) -> None:
        with self.gateway.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT a.status,r.work_order_id FROM external_code_attempts a "
                        "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
                        "WHERE a.project_id=:project_id AND a.run_id=:run_id AND a.id=:id FOR UPDATE"
                    ),
                    {"project_id": project_id, "run_id": run_id, "id": attempt_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["status"] != "queued":
                return
            connection.execute(
                text(
                    "UPDATE external_code_attempts SET status='unknown',reason_code=:reason,"
                    "provider_session_id=:provider_session_id "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "project_id": project_id,
                    "id": attempt_id,
                    "reason": reason,
                    "provider_session_id": (
                        launch_evidence["provider_session_id"] if launch_evidence else None
                    ),
                },
            )
            connection.execute(
                text(
                    "UPDATE runs SET status='unknown',outcome_reason=:reason,finished_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": run_id, "reason": reason},
            )
            self.gateway.settle_work_order(connection, project_id, row["work_order_id"], run_id, "unknown")
            self.gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                run_id,
                "external_code_unknown",
                {
                    "attempt_id": str(attempt_id),
                    "reason_code": reason,
                    **(
                        {
                            "launch_id": launch_evidence["launch_id"],
                            "launch_sha256": launch_evidence["launch_sha256"],
                            "session_observed": launch_evidence["session_observed"],
                            "session_sha256": launch_evidence.get("session_sha256"),
                        }
                        if launch_evidence
                        else {}
                    ),
                },
            )

    def _record_launch_events(
        self,
        project_id: UUID,
        run_id: UUID,
        attempt: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> None:
        if evidence is None or attempt["fixture_id"] != "github_snapshot_codex_fixture_v1":
            return
        with self.gateway.engine.begin() as connection:
            order_id = connection.scalar(
                text("SELECT work_order_id FROM runs WHERE project_id=:project_id AND id=:run_id FOR UPDATE"),
                {"project_id": project_id, "run_id": run_id},
            )
            records = [
                (
                    "external_code_prelaunch",
                    "launch_sha256",
                    evidence["launch_sha256"],
                    {
                        "attempt_id": str(attempt["id"]),
                        "launch_id": evidence["launch_id"],
                        "launch_sha256": evidence["launch_sha256"],
                        "snapshot_sha256": evidence["snapshot_sha256"],
                        "source_tree": evidence["source_tree"],
                        "snapshot_revision": evidence["snapshot_revision"],
                        "snapshot_receipt_sha256": evidence["snapshot_receipt_sha256"],
                    },
                )
            ]
            if evidence["session_observed"]:
                records.append(
                    (
                        "external_code_session",
                        "session_sha256",
                        evidence["session_sha256"],
                        {
                            "attempt_id": str(attempt["id"]),
                            "launch_id": evidence["launch_id"],
                            "launch_sha256": evidence["launch_sha256"],
                            "session_sha256": evidence["session_sha256"],
                            "provider_session_id": evidence["provider_session_id"],
                            "provider_status": evidence["provider_status"],
                        },
                    )
                )
            for event_type, digest_key, digest, payload in records:
                prior = connection.scalar(
                    text(
                        "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
                        "AND event_type=:event_type AND payload->>:digest_key=:digest LIMIT 1"
                    ),
                    {
                        "project_id": project_id,
                        "run_id": run_id,
                        "event_type": event_type,
                        "digest_key": digest_key,
                        "digest": digest,
                    },
                )
                if prior is None:
                    self.gateway._event(connection, project_id, order_id, run_id, event_type, payload)

    def _mark_invalid_when_fenced(
        self,
        project_id: UUID,
        run_id: UUID,
        attempt: dict[str, Any],
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        attempt_id: UUID = attempt["id"]
        if attempt["fixture_id"] == "github_snapshot_codex_fixture_v1":
            try:
                age = self.spool.claim_age_seconds(attempt_id)
                if age is None:
                    if not self.spool.cancel_unclaimed(attempt_id):
                        return
                elif not self.spool.read_snapshot_codex_fence(
                    attempt_id, project_id, run_id, attempt["request_sha256"]
                ):
                    return
            except (OSError, SpoolInvalid):
                return
        self._mark_unknown(project_id, run_id, attempt_id, reason, evidence)

    def reconcile(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        detail = self.detail(project_id, run_id)
        queued = [row for row in detail["attempts"] if row["status"] == "queued"]
        if not queued:
            return self.detail(project_id, run_id)
        attempt = queued[-1]
        attempt_id: UUID = attempt["id"]
        wait_seconds = self.receipt_wait_seconds
        if attempt["fixture_id"] == "repository_code_v1":
            from onefoundry.repository_code_observation import saved_manifest

            wait_seconds = saved_manifest(self, project_id, run_id).stage_limits.receipt_wait_seconds
        try:
            launch_evidence = self.spool.read_launch_evidence(
                attempt_id, project_id, run_id, attempt["request_sha256"]
            )
        except (OSError, SpoolInvalid):
            self._mark_invalid_when_fenced(project_id, run_id, attempt, "launch_record_invalid")
            return self.detail(project_id, run_id)
        if launch_evidence is not None and attempt["fixture_id"] == "github_snapshot_codex_fixture_v1":
            if (
                launch_evidence["invocation_kind"] != "synthetic_snapshot_codex_fixture"
                or launch_evidence.get("snapshot_sha256") != attempt["snapshot_sha256"]
                or launch_evidence.get("source_tree") != attempt["snapshot_tree"]
                or launch_evidence.get("snapshot_revision") != attempt["snapshot_revision"]
                or launch_evidence.get("snapshot_receipt_sha256") != attempt["snapshot_receipt_sha256"]
            ):
                self._mark_invalid_when_fenced(project_id, run_id, attempt, "launch_record_invalid")
                return self.detail(project_id, run_id)
        self._record_launch_events(project_id, run_id, attempt, launch_evidence)
        try:
            observed = self.spool.read_receipt(attempt_id)
            if observed is None:
                age = self.spool.claim_age_seconds(attempt_id)
                if age is not None and age >= wait_seconds:
                    if attempt[
                        "fixture_id"
                    ] != "github_snapshot_codex_fixture_v1" or self.spool.read_snapshot_codex_fence(
                        attempt_id, project_id, run_id, attempt["request_sha256"]
                    ):
                        self._mark_unknown(
                            project_id, run_id, attempt_id, "claimed_receipt_missing", launch_evidence
                        )
                elif (
                    age is None
                    and (datetime.now(UTC) - attempt["created_at"]).total_seconds() >= wait_seconds
                    and self.spool.cancel_unclaimed(attempt_id)
                ):
                    self._mark_unknown(project_id, run_id, attempt_id, "unclaimed_dispatch_expired")
                return self.detail(project_id, run_id)
            payload, digest = observed
            receipt = ExternalCodeReceipt.model_validate(payload)
            self._validate_receipt(project_id, run_id, attempt, receipt)
            if receipt.commit is not None and attempt["fixture_id"] == "repository_code_v1":
                from onefoundry.repository_code_observation import validate_observation

                validate_observation(self, project_id, run_id, attempt, receipt)
            if receipt.status == "done" and attempt["fixture_id"] == FIXTURE_ID:
                if (
                    launch_evidence is None
                    or not launch_evidence["session_observed"]
                    or launch_evidence["invocation_kind"]
                    not in ("trusted_local_cli_v1", "synthetic_codex_fixture", "synthetic_claude_fixture")
                ):
                    raise ValueError("coding success lacks a verified launch")
            if launch_evidence is not None:
                if (receipt.status == "done" and not launch_evidence["session_observed"]) or (
                    launch_evidence["session_observed"]
                    and (
                        receipt.provider_session_id != launch_evidence["provider_session_id"]
                        or (receipt.status == "done" and launch_evidence["provider_status"] != "done")
                    )
                ):
                    raise ValueError("receipt conflicts with launch observation")
            artifact_data: dict[str, bytes] = {}
            for artifact in receipt.artifacts:
                data = self.spool.read_artifact(
                    attempt_id, artifact.path, artifact.byte_size, artifact.sha256
                )
                if artifact.kind == "diff" and attempt["fixture_id"] == "repository_code_v1":
                    from onefoundry.repository_source_safety import SourceSafety

                    manifest = saved_manifest(self, project_id, run_id)
                    safety = SourceSafety.model_validate_json(
                        self.spool._read_regular(
                            self.spool.root / "claims" / f"{attempt_id}.source-safety.json",
                            manifest.stage_limits.max_tree_bytes,
                        )
                    )
                    safety.validate_job(manifest, str(receipt.commit), data)
                elif SECRET_PATTERN.search(data.decode("utf-8")):
                    raise ValueError("artifact appears to contain a secret")
                if artifact.kind == "trace":
                    SavedActionTrace.model_validate_json(data)
                artifact_data[artifact.kind] = data
            if attempt["fixture_id"] == "github_snapshot_codex_fixture_v1" and receipt.status == "done":
                if (
                    artifact_data.get("diff") != expected_diff()
                    or hashlib.sha256(expected_diff()).hexdigest() != receipt.diff_sha256
                    or not artifact_data.get("log", b"").startswith(
                        b"synthetic_cli=codex_cli model=gpt-5 command=snapshot_file_repair "
                    )
                ):
                    raise ValueError("synthetic snapshot coding artifacts invalid")
        except (OSError, SpoolInvalid, ValidationError, ValueError, UnicodeDecodeError):
            self._mark_invalid_when_fenced(project_id, run_id, attempt, "receipt_invalid", launch_evidence)
            return self.detail(project_id, run_id)
        with self.gateway.engine.begin() as connection:
            row: str = connection.execute(
                text(
                    "SELECT status FROM external_code_attempts "
                    "WHERE project_id=:project_id AND run_id=:run_id AND id=:id FOR UPDATE"
                ),
                {"project_id": project_id, "run_id": run_id, "id": attempt_id},
            ).scalar_one()
            if row != "queued":
                return self.detail(project_id, run_id)
            connection.execute(
                text(
                    "UPDATE external_code_attempts SET status=:status,reason_code=:reason_code,"
                    "provider_session_id=:provider_session_id,baseline_commit=:baseline_commit,"
                    "branch=:branch,commit=:commit,diff_sha256=:diff_sha256,tests=:tests,"
                    "receipt_sha256=:receipt_sha256,tokens_used=:tokens_used,cost_usd=:cost_usd,"
                    "started_at=:started_at,finished_at=:finished_at "
                    "WHERE project_id=:project_id AND id=:id"
                ).bindparams(bindparam("tests", type_=JSONB)),
                {
                    "project_id": project_id,
                    "id": attempt_id,
                    "status": receipt.status,
                    "reason_code": receipt.reason_code,
                    "provider_session_id": receipt.provider_session_id,
                    "baseline_commit": receipt.baseline_commit,
                    "branch": receipt.branch,
                    "commit": receipt.commit,
                    "diff_sha256": receipt.diff_sha256,
                    "tests": [test.model_dump() for test in receipt.tests],
                    "receipt_sha256": digest,
                    "tokens_used": receipt.tokens_used,
                    "cost_usd": receipt.cost_usd,
                    "started_at": receipt.started_at,
                    "finished_at": receipt.finished_at,
                },
            )
            for artifact in receipt.artifacts:
                connection.execute(
                    text(
                        "INSERT INTO external_code_artifacts "
                        "(id,project_id,run_id,attempt_id,actor_id,kind,path,sha256,byte_size) "
                        "VALUES (:id,:project_id,:run_id,:attempt_id,:actor_id,:kind,:path,"
                        ":sha256,:byte_size)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "actor_id": f"runner:{receipt.adapter_id}",
                        **artifact.model_dump(),
                    },
                )
            connection.execute(
                text(
                    "UPDATE runs SET status=:status,outcome_reason=:reason,finished_at=clock_timestamp(),"
                    "tokens_used=:tokens_used,cost_usd=:cost_usd WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "project_id": project_id,
                    "id": run_id,
                    "status": receipt.status,
                    "reason": receipt.reason_code,
                    "tokens_used": receipt.tokens_used if receipt.status == "done" else None,
                    "cost_usd": receipt.cost_usd if receipt.status == "done" else None,
                },
            )
            if attempt["fixture_id"] != "repository_code_v1" or receipt.status != "done":
                self.gateway.settle_work_order(
                    connection, project_id, detail["run"]["work_order_id"], run_id, receipt.status
                )
            self.gateway._event(
                connection,
                project_id,
                detail["run"]["work_order_id"],
                run_id,
                "external_code_receipt",
                {
                    "attempt_id": str(attempt_id),
                    "adapter_id": receipt.adapter_id,
                    "status": receipt.status,
                    "reason_code": receipt.reason_code,
                    "receipt_sha256": digest,
                    "diff_sha256": receipt.diff_sha256,
                    "tests": [test.model_dump() for test in receipt.tests],
                    "tokens_used": receipt.tokens_used,
                    "cost_usd": str(receipt.cost_usd) if receipt.cost_usd is not None else None,
                },
            )
        return self.detail(project_id, run_id)

    @staticmethod
    def _validate_receipt(
        project_id: UUID,
        run_id: UUID,
        attempt: dict[str, Any],
        receipt: ExternalCodeReceipt,
    ) -> None:
        if (
            receipt.project_id != project_id
            or receipt.run_id != run_id
            or receipt.job_id != attempt["id"]
            or receipt.attempt_id != attempt["id"]
            or receipt.adapter_id != attempt["adapter_id"]
            or receipt.request_sha256 != attempt["request_sha256"]
            or receipt.finished_at < receipt.started_at
        ):
            raise ValueError("receipt identity mismatch")
        if attempt["fixture_id"] == FIXTURE_ID:
            if receipt.schema_version != 1 or receipt.fixture_id != FIXTURE_ID:
                raise ValueError("receipt target mismatch")
        elif attempt["fixture_id"] == "local_source_probe_v1":
            expected_source = {
                "source_id": str(attempt["source_id"]),
                "root_id": attempt["source_root_id"],
                "relative_path": attempt["source_relative_path"],
                "branch": attempt["source_branch"],
                "revision": attempt["source_revision"],
            }
            if (
                receipt.schema_version != 2
                or receipt.target_id != "local_source_probe_v1"
                or receipt.source is None
                or receipt.source.model_dump(mode="json") != expected_source
                or receipt.provider_session_id is not None
                or receipt.tokens_used != 0
                or receipt.cost_usd != 0
            ):
                raise ValueError("receipt source snapshot mismatch")
            probe_checks = [(item.name, item.status, item.exit_code) for item in receipt.tests]
            if receipt.status == "done" and probe_checks != [("git_diff_check", "passed", 0)]:
                raise ValueError("successful probe is missing its passed diff check")
            if receipt.status != "done" and probe_checks:
                raise ValueError("unsuccessful probe carries test success evidence")
        elif attempt["fixture_id"] == "repository_code_v1":
            expected_source = {
                "source_id": str(attempt["source_id"]),
                "root_id": attempt["source_root_id"],
                "relative_path": attempt["source_relative_path"],
                "branch": attempt["source_branch"],
                "revision": attempt["source_revision"],
            }
            if (
                receipt.schema_version != 4
                or receipt.source is None
                or receipt.target_id != "repository_code_v1"
                or receipt.source.model_dump(mode="json") != expected_source
                or receipt.idempotency_key != attempt["idempotency_key"]
                or receipt.intent_sha256 != attempt["intent_sha256"]
            ):
                raise ValueError("maintenance receipt binding mismatch")
            before = [test for test in receipt.tests if test.name.startswith("baseline_")]
            after = [test for test in receipt.tests if test.name.startswith("candidate_")]
            if receipt.status == "done" and (
                not before
                or len(before) != len(after)
                or not any(test.status == "failed" for test in before)
                or any(test.status != "passed" or test.exit_code != 0 for test in after)
                or receipt.provider_session_id is None
            ):
                raise ValueError("maintenance receipt lacks reproduced gap and passed candidate")
        elif attempt["fixture_id"] == "github_snapshot_codex_fixture_v1":
            expected_snapshot = {
                "snapshot_attempt_id": str(attempt["snapshot_attempt_id"]),
                "source_id": str(attempt["snapshot_source_id"]),
                "account_id": str(attempt["snapshot_account_id"]),
                "owner": attempt["snapshot_owner"],
                "repo": attempt["snapshot_repo"],
                "branch": attempt["snapshot_branch"],
                "saved_revision": attempt["snapshot_revision"],
                "source_tree": attempt["snapshot_tree"],
                "snapshot_path": attempt["snapshot_path"],
                "snapshot_sha256": attempt["snapshot_sha256"],
                "snapshot_byte_size": attempt["snapshot_byte_size"],
                "snapshot_request_sha256": attempt["snapshot_request_sha256"],
                "snapshot_receipt_sha256": attempt["snapshot_receipt_sha256"],
            }
            if (
                receipt.schema_version != 3
                or receipt.target_id != "github_snapshot_codex_fixture_v1"
                or receipt.snapshot is None
                or receipt.idempotency_key != attempt["idempotency_key"]
                or receipt.intent_sha256 != attempt["intent_sha256"]
                or any(
                    receipt.snapshot.model_dump(mode="json")[key] != value
                    for key, value in expected_snapshot.items()
                )
                or receipt.cost_usd is not None
                or (
                    receipt.baseline_commit is not None
                    and receipt.baseline_commit == attempt["snapshot_revision"]
                )
            ):
                raise ValueError("synthetic snapshot coding receipt binding mismatch")
            checks = [(item.name, item.status, item.exit_code) for item in receipt.tests]
            if receipt.status == "done" and (
                checks != [("file_text_before", "failed", 1), ("file_text_after", "passed", 0)]
                or receipt.tokens_used != 28
                or receipt.provider_session_id is None
            ):
                raise ValueError("synthetic snapshot coding check missing")
            if receipt.status != "done" and checks:
                raise ValueError("unsuccessful synthetic coding receipt carries passing checks")
        else:
            raise ValueError("unsupported coding target")
        if receipt.provider_session_id is not None and (
            not receipt.provider_session_id.strip()
            or SECRET_PATTERN.search(receipt.provider_session_id)
            or any(ord(char) < 32 for char in receipt.provider_session_id)
        ):
            raise ValueError("invalid provider session ID")
        paths = {item.kind: item.path for item in receipt.artifacts}
        if len(paths) != len(receipt.artifacts):
            raise ValueError("duplicate coding artifact kind")
        trace_path = f"artifacts/{attempt['id']}/actions.json"
        if "trace" in paths and (attempt["fixture_id"] != FIXTURE_ID or paths["trace"] != trace_path):
            raise ValueError("unexpected coding trace artifact")
        failed_candidate = (
            attempt["fixture_id"] == "repository_code_v1"
            and receipt.status == "failed"
            and receipt.reason_code == "maintenance_candidate_checks_failed"
            and receipt.commit is not None
        )
        if failed_candidate:
            from onefoundry.repository_failure_evidence import validate_failed_checks

            validate_failed_checks(receipt)
        if receipt.status == "done" or failed_candidate:
            expected = {
                "diff": f"artifacts/{attempt['id']}/change.diff",
                "log": f"artifacts/{attempt['id']}/after.log",
            }
            if "trace" in paths:
                expected["trace"] = trace_path
            if (
                paths != expected
                or receipt.baseline_commit is None
                or receipt.commit is None
                or receipt.diff_sha256 is None
                or receipt.branch
                != (
                    f"fixture/repair-{attempt['id']}"
                    if attempt["fixture_id"] == FIXTURE_ID
                    else f"onefoundry/probe-{attempt['id']}"
                    if attempt["fixture_id"] == "local_source_probe_v1"
                    else f"onefoundry/extension-{attempt['id']}"
                    if attempt["fixture_id"] == "repository_code_v1"
                    else f"fixture/snapshot-{attempt['id']}"
                )
                or not (
                    BRANCH
                    if attempt["fixture_id"] == FIXTURE_ID
                    else PROBE_BRANCH
                    if attempt["fixture_id"] == "local_source_probe_v1"
                    else re.compile(r"^onefoundry/extension-[0-9a-f-]{36}$")
                    if attempt["fixture_id"] == "repository_code_v1"
                    else SNAPSHOT_BRANCH
                ).fullmatch(receipt.branch)
                or receipt.diff_sha256
                != next(item.sha256 for item in receipt.artifacts if item.kind == "diff")
            ):
                raise ValueError("successful receipt missing exact candidate")
        elif (
            receipt.branch is not None
            or receipt.commit is not None
            or receipt.diff_sha256 is not None
            or any(item.kind != "trace" for item in receipt.artifacts)
        ):
            raise ValueError("non-success receipt carries candidate")

    def preview(self, project_id: UUID, run_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        from onefoundry.gateway_errors import Conflict, NotFound

        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT a.kind,a.path,a.sha256,a.byte_size,a.attempt_id "
                        "FROM external_code_artifacts a JOIN runs r ON "
                        "r.project_id=a.project_id AND r.id=a.run_id "
                        "WHERE a.project_id=:project_id AND a.run_id=:run_id AND a.id=:id "
                        "AND r.mode='external_code'"
                    ),
                    {"project_id": project_id, "run_id": run_id, "id": artifact_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("artifact not found")
        try:
            data = self.spool.read_artifact(row["attempt_id"], row["path"], row["byte_size"], row["sha256"])
            return {
                "kind": row["kind"],
                "sha256": row["sha256"],
                "byte_size": row["byte_size"],
                "text": data.decode("utf-8"),
            }
        except (OSError, SpoolInvalid, UnicodeDecodeError) as exc:
            raise Conflict("artifact readback failed") from exc
