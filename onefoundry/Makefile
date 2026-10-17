.PHONY: check format check-extension-isolation check-onefoundry-capability check-onefoundry-records check-onefoundry-manual check-onefoundry-event check-onefoundry-inbox
UV := $(shell command -v uv || printf .bootstrap-venv/bin/uv)

check:
	$(UV) run --locked pytest -q sdk/python/tests
	$(UV) run --locked mypy --config-file sdk/python/pyproject.toml sdk/python/src sdk/python/tests
	$(UV) build sdk/python --offline --out-dir output/sdk-build-check
	$(UV) run --locked pytest -q tests/test_gateway_extension_identity.py tests/test_gateway_extension_settings.py tests/test_gateway_extension_transport.py tests/test_gateway_extension_worker_runtime.py
	$(UV) run --locked pytest -q tests/test_gateway_extension_credentials.py tests/test_gateway_extension_spool.py tests/test_gateway_extension_worker_readiness.py tests/test_webhook_origin.py tests/test_check_extension_isolation.py tests/test_extension_worker_child.py tests/test_extension_worker_host.py tests/test_extension_worker_supervisor.py tests/test_extension_fixture_test.py
	$(UV) run --locked pytest -q tests/test_git_hygiene.py tests/test_install_gitleaks.py
	$(UV) run --locked pytest -q tests/test_repository_failure_evidence.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_repository_source_safety.py
	$(UV) run --locked pytest -q tests/test_repository_read_extensions.py tests/test_team_workflow_admission.py
	$(UV) run --locked pytest -q tests/test_repository_provider_response.py tests/test_repository_overload_recovery.py
	$(UV) run --locked pytest -q tests/test_capability_setup_host.py tests/test_repository_subscription.py tests/test_repository_github.py tests/test_extension_release_prepare.py tests/test_extension_source_checkout.py tests/test_maintenance_transport_config.py
	$(UV) run --locked pytest -q tests/test_repository_code.py tests/test_repository_code_ignored.py tests/test_repository_source_view.py tests/test_repository_provider.py tests/test_repository_codex_observation.py tests/test_repository_candidate_observation.py tests/test_repository_provider_diagnostics.py tests/test_extension_activation.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_runtime_maintenance_http.py
	$(UV) run --locked pytest -q tests/test_repository_transport.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_capability_inspection.py tests/test_core_feature_defaults.py
	$(UV) run --locked pytest -q tests/test_video_edit_contracts.py tests/test_video_edit_settings.py tests/test_video_edit_supervisor.py tests/test_video_edit_lifecycle.py
	$(UV) run --locked python -m scripts.release_bundle verify-source
	$(UV) run --locked ruff format --check .
	$(UV) run --locked ruff check .
	$(UV) run --locked mypy
	$(UV) run --locked pytest -q tests/test_input_file_bytes.py tests/test_input_file_context.py tests/test_input_file_extract.py tests/test_input_file_settings.py
	$(UV) run --locked pytest -q tests/test_release_update.py tests/test_operator_cli.py
	$(UV) run --locked pytest -q tests/test_task_automation_prompt.py
	$(UV) run --locked pytest -q tests/test_chief_visibility.py tests/test_agent_conversation_policy.py tests/test_long_operator_messages.py
	$(UV) run --locked pytest -q tests/test_team_workflow_admission.py tests/test_task_session_report_contract.py tests/test_media_capture_contracts.py tests/test_team_dependency_pages.py tests/test_demo_native_configuration.py
	$(UV) run --locked pytest -q tests/test_onefoundry_subject.py tests/test_onefoundry_next_action.py tests/test_http.py tests/test_settings.py tests/test_public_web_client.py tests/test_role_coding_evidence_unit.py tests/test_role_repository_file_unit.py tests/test_role_coding_artifact_unit.py tests/test_github_pr_transport.py tests/test_live_tool_sdk.py tests/test_task_session_sdk_turn.py tests/test_task_session_role_recovery.py tests/test_role_sensitive_text.py tests/test_model_gateway_unit.py tests/test_provider_directory.py tests/test_inference_transports.py tests/test_subscription_inference.py tests/test_codex_account.py tests/test_grok_account.py tests/test_grok_gateway_lifecycle.py tests/test_project_sources.py tests/test_local_source_stage.py tests/test_github_fixture_stage.py tests/test_github_https_fixture_fetch.py tests/test_github_snapshot_bundle.py tests/test_github_fixture_snapshot_materialize.py tests/test_local_source_probe.py tests/test_host_probe_bridge.py tests/test_scripted_text_sdk.py tests/test_runner.py tests/test_external_cli.py tests/test_external_service.py tests/test_action_trace.py tests/test_isolated_cli.py tests/test_codex_app_server.py tests/test_claude_code.py tests/test_infra_monitor.py tests/test_synthetic_memory.py tests/test_sales_readback.py tests/test_skill_import_pack.py tests/test_rename_skill_library.py tests/test_local_repositories.py tests/test_discord_dm.py tests/test_remote_channel_runtime.py tests/test_weixin_ilink.py tests/test_browser_target.py tests/test_browser_fixture_proof.py tests/test_github_pr_comment_contract.py tests/test_role_github_pr_comment_unit.py tests/test_browser_static_job_spool.py tests/test_browser_static_host_bridge.py tests/test_role_decimal_calculate.py tests/test_artifact_workspace.py tests/test_coordination_schedule.py tests/test_coordination_rule_limits.py tests/test_mail_imap.py tests/test_mail_idle.py tests/test_connectors_unit.py tests/test_documents_unit.py tests/test_document_operations_unit.py
	$(UV) run --locked pytest -q tests/test_smtp_unit.py tests/test_zoho_mail_unit.py tests/test_zoho_oauth_unit.py
	$(UV) run --locked pytest -q tests/test_mail_plugin_contracts.py tests/test_mail_plugin_settings.py tests/test_mail_plugin_auth.py tests/test_mail_plugin_imap.py tests/test_mail_plugin_service.py tests/test_mail_plugin_package.py
	$(UV) run --locked pytest -q tests/test_codex_workorder_fixture.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_github_snapshot_codex_fixture_proof.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_github_snapshot_codex_service.py tests/test_github_snapshot_codex_supervisor.py
	$(UV) run --locked pytest -q tests/test_claude_workorder_fixture.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_codex_broker_proof.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_codex_team_appserver_proof.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_codex_team_broker_policy.py
	$(UV) run --locked pytest -q tests/test_codex_team_broker_child.py
	$(UV) run --locked pytest -q tests/test_external_offline_supervisor.py tests/test_trusted_local_cli.py tests/test_provider_progress.py tests/test_live_progress_spool.py tests/test_external_code_progress_api.py tests/test_isolated_fixture_test.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_github_stage_supervisor.py
	$(UV) run --locked pytest -q tests/test_browser_static_supervisor.py -m "not integration"
	$(UV) run --locked pytest -q tests/test_github_fixture_https_supervisor.py
	$(UV) run --locked pytest -q tests/test_github_fixture_snapshot_supervisor.py
	$(UV) run --locked pytest -q tests/test_memory_embeddings.py tests/test_company_memory_contracts.py tests/test_memory_capture_api_key.py tests/test_memory_plugin.py tests/test_memory_plugin_api_key.py tests/test_memory_plugin_bridge.py tests/test_memory_local_runtime.py tests/test_memory_plugin_package.py tests/test_memory_companion_settings.py tests/test_memory_plugin_db_role.py
	$(MAKE) check-extension-isolation
	./scripts/check_postgres.sh "$(UV)"
	npm --prefix frontend ci
	npm --prefix frontend run lint
	npm --prefix frontend run test
	npm --prefix frontend run build

format:
	$(UV) run --locked ruff format .

check-extension-isolation:
	$(UV) run --locked python scripts/check_extension_isolation.py

# Requires the disposable PostgreSQL test environment used by check_postgres.sh.
check-onefoundry-capability:
	$(UV) run --locked pytest -q tests/test_onefoundry_capability_postgres.py
	npm --prefix frontend run test -- onefoundryCapability.test.tsx

# Requires the disposable PostgreSQL test environment used by check_postgres.sh.
check-onefoundry-records:
	$(UV) run --locked pytest -q tests/test_onefoundry_organizations_postgres.py tests/test_onefoundry_organization_api_postgres.py
	npm --prefix frontend run test -- onefoundryOrganizations.test.tsx

check-onefoundry-manual:
	$(UV) run --locked pytest -q tests/test_onefoundry_context_postgres.py tests/test_onefoundry_budget_postgres.py tests/test_onefoundry_draft_postgres.py
	npm --prefix frontend run test -- onefoundryManualFlow.test.tsx

check-onefoundry-event:
	$(UV) run --locked pytest -q tests/test_onefoundry_intent_postgres.py
	npm --prefix frontend run test -- onefoundryEventFlow.test.tsx

check-onefoundry-inbox:
	$(UV) run --locked pytest -q tests/test_onefoundry_inbox_postgres.py
	npm --prefix frontend run test -- onefoundryInbox.test.tsx
