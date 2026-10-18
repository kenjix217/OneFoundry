"""Provision the narrow database login used by the HTTP process."""

import re

from psycopg import connect, sql
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from onefoundry.settings import Settings


class RuntimeRoleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="forbid")

    app_db_user: str = "onefoundry_app"
    app_db_password: str = Field(default="", min_length=1, repr=False)

    @field_validator("app_db_password")
    @classmethod
    def reject_blank_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("APP_DB_PASSWORD must not be blank")
        return value

    @field_validator("app_db_user")
    @classmethod
    def validate_role_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value) or value.startswith("pg_"):
            raise ValueError("APP_DB_USER must be a safe lowercase PostgreSQL role name")
        return value


def provision_runtime_role(bootstrap: Settings, runtime: RuntimeRoleSettings) -> None:
    if runtime.app_db_user.casefold() == bootstrap.db_user.casefold():
        raise ValueError("APP_DB_USER must differ from the bootstrap DB_USER")
    conninfo = bootstrap.database_url.set(drivername="postgresql").render_as_string(hide_password=False)
    role = sql.Identifier(runtime.app_db_user)
    with connect(conninfo) as connection:
        existing = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = %s",
            (runtime.app_db_user,),
        ).fetchone()
        if existing is not None and any(existing):
            raise ValueError("APP_DB_USER refers to an existing privileged role")
        if existing is None:
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
        connection.execute(
            sql.SQL(
                "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(role, sql.Literal(runtime.app_db_password))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(bootstrap.db_name), role)
        )
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        connection.execute(
            sql.SQL(
                "REVOKE ALL ON TABLE projects, profiles, model_accounts, model_catalog, "
                "live_text_run_bindings, live_text_outputs, live_text_output_sources, "
                "task_conversations, task_conversation_turns, task_sessions, "
                "task_session_messages, task_session_attempts, task_session_assignments, "
                "task_session_dependencies, chief_conversation_controls, project_chief_turns, "
                "task_automation_decisions, "
                "task_assignment_links, chief_followup_intents, "
                "remote_channels, remote_pairing_challenges, remote_inbound, remote_outbound, "
                "events, work_orders, runs, "
                "run_profile_snapshots, "
                "approval_requests, run_results, dreamcycle_candidates, memory_items, memory_captures, "
                "text_outputs, text_output_sources, runner_jobs, runner_artifacts, "
                "synthetic_merge_bindings, sales_drafts, skills, skill_versions, "
                "profile_skill_bindings, run_skill_snapshots, external_code_attempts, "
                "external_code_artifacts, browser_static_fixtures, browser_static_jobs, "
                "github_accounts, project_sources, "
                "github_fixture_source_snapshots, workspace_artifacts, video_edit_assets, video_edit_jobs, "
                "input_files, input_file_chunks, input_file_links, input_file_context_uses, "
                "onefoundry_organizations, onefoundry_sources, onefoundry_claims, "
                "onefoundry_workflows, onefoundry_work_order_subjects, "
                "onefoundry_company_statements, onefoundry_budget_snapshots, "
                "onefoundry_budget_reservations, onefoundry_context_packets, onefoundry_drafts, "
                "coordination_intents, coordination_signals, coordination_rules, "
                "coordination_rule_versions, coordination_controls, mail_accounts, "
                "coordination_profile_proposals, connector_accounts, connector_signouts, "
                "document_sources, document_files, "
                "document_writes, document_actions, smtp_accounts, outbound_emails, "
                "zoho_mail_connections, zoho_mail_actions, project_plans, "
                "extension_packages, extension_connections, extension_drafts, "
                "extension_skill_prerequisites, extension_package_skills, "
                "extension_review_bindings, extension_run_tools, "
                "extension_connection_credentials, extension_invocations, extension_connection_bindings, "
                "extension_webhooks, extension_webhook_deliveries FROM {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE chief_conversation_controls TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (version,archived,paused,trashed) ON TABLE chief_conversation_controls TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE projects TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE project_plans TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT UPDATE (body, version, actor_id, updated_at) ON TABLE project_plans TO {}").format(
                role
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, version, trash_from_status, goal, desired_outcome) "
                "ON TABLE projects TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE profiles TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE skills TO {}").format(role))
        connection.execute(sql.SQL("GRANT UPDATE (name, description) ON TABLE skills TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE skill_versions TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, DELETE ON TABLE profile_skill_bindings TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (id, version, actor_id, created_at) ON TABLE profile_skill_bindings TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE run_skill_snapshots TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT ON TABLE extension_packages, extension_connections, "
                "extension_drafts, extension_skill_prerequisites, extension_package_skills, "
                "extension_review_bindings, extension_run_tools, "
                "extension_connection_credentials, extension_invocations, extension_connection_bindings, "
                "extension_webhooks, extension_webhook_deliveries TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (name,enabled,version,credential_id,updated_at) "
                "ON TABLE extension_webhooks TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (connection_id,actor_id,version) ON TABLE extension_connection_bindings TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT UPDATE (state,revision) ON TABLE extension_packages TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT UPDATE (config,enabled,version) ON TABLE extension_connections TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (actor_id,credential_id,origin,auth_method,header_name) "
                "ON TABLE extension_connection_credentials TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status,result,reason,finished_at,waiting_invocation_id,worker_resume_from,"
                "worker_resume_started_at,started_at) "
                "ON TABLE extension_invocations TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status,accepted_skill_id,accepted_skill_version) "
                "ON TABLE extension_drafts TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (model, model_account_id, reasoning_effort, instructions, "
                "memory_scope, allowed_tools, "
                "autonomy_level, configured, reports_to_profile_id) ON TABLE profiles TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE events TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE browser_static_fixtures TO {}").format(role)
        )
        # PostgreSQL requires UPDATE privilege to acquire SELECT ... FOR UPDATE;
        # the immutable fixture trigger still rejects actual writes.
        connection.execute(sql.SQL("GRANT UPDATE (id) ON TABLE browser_static_fixtures TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE browser_static_jobs TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, reason_code, receipt_sha256, title, inspection_text, "
                "blocked_request_count, finished_at) ON TABLE browser_static_jobs TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT, DELETE ON TABLE model_accounts TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (catalog_valid, credential_revision, credential_ready, "
                "refresh_generation, last_refreshed_at) "
                "ON TABLE model_accounts TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT, DELETE ON TABLE model_catalog TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE work_orders TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, profile_id, block_reason, blocked_from_status, "
                "title, instructions) "
                "ON TABLE work_orders TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE runs TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE live_text_run_bindings TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE task_conversations TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE task_sessions TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, provider_thread_id, execution_mode) ON TABLE task_sessions TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE task_session_messages TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE task_session_attempts TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE task_session_assignments TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, result_message_id, result_action_id, result_sha256) "
                "ON TABLE task_session_assignments TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE task_session_dependencies TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE task_session_decision_causes TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE task_session_tool_calls TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, result, reason, finished_at) ON TABLE task_session_tool_calls TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (dispatch_attempt_id, resolved_at, handled_at) "
                "ON TABLE task_session_decision_causes TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, provider, model_id, provider_thread_id, provider_turn_id, "
                "prompt_sha256, context_message_ids, context_dependency_ids, "
                "context_omitted_count, context_memory, input_sequence, input_tokens, output_tokens, "
                "cost_usd, reason, "
                "started_at, finished_at) "
                "ON TABLE task_session_attempts TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT UPDATE (is_active) ON TABLE task_conversations TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE project_chief_turns TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status,assistant_text,assistant_sha256,reason,action,"
                "target_work_order_id,created_work_order_id,created_rule_id,account_id,provider,model_id,"
                "provider_response_id,input_tokens,output_tokens,candidate_work_order_ids,finished_at) "
                "ON TABLE project_chief_turns TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE remote_channels TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (enabled, reply_policy, sender_id, chat_id, bot_id, guild_id, "
                "observed_sender_id, "
                "observed_chat_id, observed_at, identity_confirmed, work_order_id, profile_id, "
                "credential_revision, credential_ready, connected, last_heartbeat_at, last_inbound_at, "
                "last_outbound_at, last_error_code, poll_cursor) ON TABLE remote_channels TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE remote_pairing_challenges TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT UPDATE (confirmed_at, expires_at) ON TABLE {} TO {}").format(
                sql.Identifier("remote_pairing_challenges"), role
            )
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE remote_inbound TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT UPDATE (status, reason, finished_at) ON TABLE remote_inbound TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE remote_outbound TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, reason, remote_message_id, claimed_at, finished_at) "
                "ON TABLE remote_outbound TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE task_conversation_turns TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, assistant_text, assistant_sha256, assistant_actor_id, reason, "
                "provider_response_id, input_tokens, output_tokens, cost_usd, finished_at) "
                "ON TABLE task_conversation_turns TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE live_text_outputs TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE live_text_output_sources TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE run_profile_snapshots TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, profile_id, finished_at, outcome_reason, tokens_used, cost_usd) "
                "ON TABLE runs TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE approval_requests TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT UPDATE (status, decided_at) ON TABLE approval_requests TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE run_results TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE dreamcycle_candidates TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, reviewed_at, reviewed_by_actor_id) "
                "ON TABLE dreamcycle_candidates TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE memory_items TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE memory_embeddings TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE memory_captures TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, superseded_by, changed_at, changed_by_actor_id) "
                "ON TABLE memory_items TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE text_outputs TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE text_output_sources TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE runner_jobs TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, reason_code, branch, commit, baseline_commit, "
                "merged_commit, observed_head_commit, diff_sha256, tests, receipt_sha256, started_at, "
                "finished_at) ON TABLE runner_jobs TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE runner_artifacts TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE zoho_mail_connections, zoho_mail_actions TO {}").format(
                role
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status,enabled,mail_account_id,from_address,last_error_code,"
                "last_probe_at,version,credential_revision) "
                "ON TABLE zoho_mail_connections TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state,error_code,message_id,finished_at) ON TABLE zoho_mail_actions TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE external_code_attempts TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, reason_code, provider_session_id, baseline_commit, branch, "
                "commit, diff_sha256, tests, receipt_sha256, tokens_used, cost_usd, started_at, finished_at) "
                "ON TABLE external_code_attempts TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE external_code_artifacts TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE video_edit_assets TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE video_edit_jobs TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, attempt, artifact_id, receipt, reason, started_at, finished_at) "
                "ON TABLE video_edit_jobs TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE workspace_artifacts TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT UPDATE (status, finished_at) ON TABLE workspace_artifacts TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE input_files TO {}").format(role))
        for table in (
            "onefoundry_organizations",
            "onefoundry_sources",
            "onefoundry_claims",
            "onefoundry_workflows",
            "onefoundry_work_order_subjects",
            "onefoundry_company_statements",
            "onefoundry_budget_snapshots",
            "onefoundry_budget_reservations",
            "onefoundry_context_packets",
            "onefoundry_drafts",
            "coordination_intents",
            "coordination_signals",
            "coordination_rules",
            "coordination_rule_versions",
            "coordination_controls",
            "task_automation_decisions",
            "task_assignment_links",
            "chief_followup_intents",
            "mail_accounts",
            "coordination_profile_proposals",
            "connector_accounts",
            "connector_signouts",
            "document_sources",
            "document_files",
            "document_writes",
            "document_actions",
            "smtp_accounts",
            "outbound_emails",
        ):
            connection.execute(
                sql.SQL("GRANT SELECT, INSERT ON TABLE {} TO {}").format(sql.Identifier(table), role)
            )
        connection.execute(
            sql.SQL("GRANT UPDATE (qualification, revision) ON TABLE onefoundry_organizations TO {}").format(
                role
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, revision, blocked_reason) ON TABLE onefoundry_workflows TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, approved_by, approved_at, retired_by, retired_at) "
                "ON TABLE onefoundry_company_statements TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, owner_id, observed_tokens, observed_cost_usd) "
                "ON TABLE onefoundry_budget_reservations TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, lease_until, attempts, error_code, work_order_id, due_at, updated_at, "
                "decision, conversation_id, model_admitted_at) ON TABLE coordination_intents TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, error_code, conversation_id, turn_id, attempts, "
                "lease_until, updated_at) "
                "ON TABLE chief_followup_intents TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT UPDATE (state, reason, processed_at) ON TABLE coordination_signals TO {}").format(
                role
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (name, enabled, current_version, version, updated_at) "
                "ON TABLE coordination_rules TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (paused, version, actor_id, updated_at, triage_work_order_id, "
                "profile_tool_ceiling, agent_rules_enabled) "
                "ON TABLE coordination_controls TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT UPDATE (superseded_by, revision) ON TABLE onefoundry_sources TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (review_state, superseded_by, revision) ON TABLE onefoundry_claims TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, mime_type, reason_code, extractor_version, finished_at) "
                "ON TABLE input_files TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE input_file_chunks TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE input_file_links TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE input_file_context_uses TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE synthetic_merge_bindings TO {}").format(role)
        )
        connection.execute(sql.SQL("GRANT SELECT, INSERT ON TABLE sales_drafts TO {}").format(role))
        connection.execute(sql.SQL("GRANT DELETE ON TABLE mail_accounts TO {}").format(role))
        connection.execute(sql.SQL("GRANT DELETE ON TABLE connector_accounts TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT UPDATE (state,error_code,finished_at) ON TABLE connector_signouts TO {}").format(
                role
            )
        )
        connection.execute(sql.SQL("GRANT DELETE ON TABLE document_sources TO {}").format(role))
        connection.execute(sql.SQL("GRANT DELETE ON TABLE document_files TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state,error_code,readback,finished_at) ON TABLE document_actions TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, error_code, provider_file_id, readback, finished_at) "
                "ON TABLE document_writes TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (enabled, status, last_error_code, folder_id, folder_name, cursor, "
                "last_probe_at, "
                "last_poll_at, poll_lease_until, version) ON TABLE document_sources TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (name, revision, input_file_id, updated_at) ON TABLE document_files TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (status, last_error_code, granted_scopes, connected_at, version) "
                "ON TABLE connector_accounts TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, profile_id, first_work_order_id, decided_at) "
                "ON TABLE coordination_profile_proposals TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (enabled, status, last_error_code, capabilities, uidvalidity, last_uid, "
                "last_probe_at, last_poll_at, poll_lease_until, version, cursor) ON TABLE mail_accounts TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (enabled, status, last_error_code, last_probe_at, version) "
                "ON TABLE smtp_accounts TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT UPDATE (state, reason_code, attempted_at, finished_at) ON TABLE outbound_emails TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, DELETE ON TABLE github_accounts TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, DELETE ON TABLE project_sources TO {}").format(role)
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE github_fixture_source_snapshots TO {}").format(role)
        )
        # PostgreSQL requires UPDATE privilege for SELECT ... FOR KEY SHARE.
        # The table trigger rejects every UPDATE; this grant permits only row locking.
        connection.execute(sql.SQL("GRANT UPDATE (id) ON TABLE project_sources TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT UPDATE (delivery_status, sent_at) ON TABLE sales_drafts TO {}").format(role)
        )


if __name__ == "__main__":
    provision_runtime_role(Settings(), RuntimeRoleSettings())
