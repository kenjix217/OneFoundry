"""Settings validation and environment precedence."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from onefoundry.db_roles import RuntimeRoleSettings, provision_runtime_role
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway, Unavailable


def test_password_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("field", "default", "invalid"),
    [
        ("document_search_max_pages", 5, 21),
        ("document_search_max_results", 20, 101),
        ("document_max_range_cells", 1000, 10001),
    ],
)
def test_document_operation_bounds_and_precedence(
    monkeypatch: pytest.MonkeyPatch, field: str, default: int, invalid: int
) -> None:
    monkeypatch.delenv(field.upper(), raising=False)
    assert getattr(Settings(db_password="test-only"), field) == default
    monkeypatch.setenv(field.upper(), "2")
    assert getattr(Settings(db_password="test-only"), field) == 2
    explicit = (
        Settings(db_password="test-only", document_search_max_pages=default)
        if field == "document_search_max_pages"
        else Settings(db_password="test-only", document_search_max_results=default)
        if field == "document_search_max_results"
        else Settings(db_password="test-only", document_max_range_cells=default)
    )
    assert getattr(explicit, field) == default
    for bad in ("0", str(invalid), "invalid"):
        monkeypatch.setenv(field.upper(), bad)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_mail_idle_defaults_bounds_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIL_IDLE_ENABLED", raising=False)
    monkeypatch.delenv("MAIL_IDLE_WAIT_SECONDS", raising=False)
    assert Settings(db_password="test-only").mail_idle_enabled
    assert Settings(db_password="test-only").mail_idle_wait_seconds == 60
    monkeypatch.setenv("MAIL_IDLE_ENABLED", "false")
    monkeypatch.setenv("MAIL_IDLE_WAIT_SECONDS", "30")
    assert not Settings(db_password="test-only").mail_idle_enabled
    assert Settings(db_password="test-only").mail_idle_wait_seconds == 30
    assert (
        Settings(
            db_password="test-only", mail_idle_enabled=True, mail_idle_wait_seconds=20
        ).mail_idle_wait_seconds
        == 20
    )
    for bad in ("4", "121", "bad"):
        monkeypatch.setenv("MAIL_IDLE_WAIT_SECONDS", bad)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")
    monkeypatch.setenv("MAIL_IDLE_WAIT_SECONDS", "60")
    monkeypatch.setenv("MAIL_IDLE_ENABLED", "bad")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_onefoundry_mode_default_environment_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEFOUNDRY_BUSINESS_MODE", raising=False)
    assert Settings(db_password="test-only").onefoundry_business_mode == "off"
    monkeypatch.setenv("ONEFOUNDRY_BUSINESS_MODE", "records")
    assert Settings(db_password="test-only").onefoundry_business_mode == "records"
    assert Settings(db_password="test-only", onefoundry_business_mode="off").onefoundry_business_mode == "off"
    monkeypatch.setenv("ONEFOUNDRY_BUSINESS_MODE", "manual")
    assert Settings(db_password="test-only").onefoundry_business_mode == "manual"
    monkeypatch.setenv("ONEFOUNDRY_BUSINESS_MODE", "future")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_live_text_prompt_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_TEXT_MAX_PROMPT_BYTES", raising=False)
    assert Settings(db_password="test-only").live_text_max_prompt_bytes == 96_000
    monkeypatch.setenv("LIVE_TEXT_MAX_PROMPT_BYTES", "128000")
    assert Settings(db_password="test-only").live_text_max_prompt_bytes == 128_000
    assert (
        Settings(db_password="test-only", live_text_max_prompt_bytes=64_000).live_text_max_prompt_bytes
        == 64_000
    )
    monkeypatch.setenv("LIVE_TEXT_MAX_PROMPT_BYTES", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_task_chat_timeout_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TASK_CHAT_TIMEOUT_SECONDS", raising=False)
    assert Settings(db_password="test-only").task_chat_timeout_seconds == 120
    monkeypatch.setenv("TASK_CHAT_TIMEOUT_SECONDS", "180")
    assert Settings(db_password="test-only").task_chat_timeout_seconds == 180
    assert Settings(db_password="test-only", task_chat_timeout_seconds=90).task_chat_timeout_seconds == 90
    monkeypatch.setenv("TASK_CHAT_TIMEOUT_SECONDS", "601")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


@pytest.mark.parametrize(
    ("field", "environment", "default", "override", "invalid"),
    [
        ("agent_automation_context_max_items", "AGENT_AUTOMATION_CONTEXT_MAX_ITEMS", 8, 12, 0),
        ("agent_automation_goal_max_chars", "AGENT_AUTOMATION_GOAL_MAX_CHARS", 500, 800, 50),
    ],
)
def test_agent_automation_limits_default_override_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    environment: str,
    default: int,
    override: int,
    invalid: int,
) -> None:
    monkeypatch.delenv(environment, raising=False)
    assert getattr(Settings(db_password="test-only"), field) == default
    monkeypatch.setenv(environment, str(override))
    assert getattr(Settings(db_password="test-only"), field) == override
    explicit = (
        Settings(db_password="test-only", agent_automation_context_max_items=default)
        if field == "agent_automation_context_max_items"
        else Settings(db_password="test-only", agent_automation_goal_max_chars=default)
    )
    assert getattr(explicit, field) == default
    monkeypatch.setenv(environment, str(invalid))
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_onefoundry_budget_defaults_bounds_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEFOUNDRY_MAX_MODEL_ATTEMPTS", raising=False)
    defaults = Settings(db_password="test-only")
    assert (
        defaults.onefoundry_max_linked_orders,
        defaults.onefoundry_max_model_attempts,
        defaults.onefoundry_max_public_web_calls,
        defaults.onefoundry_max_context_bytes,
        defaults.onefoundry_max_output_tokens,
        defaults.onefoundry_max_elapsed_seconds,
    ) == (
        2,
        2,
        0,
        None,
        512,
        600,
    )
    monkeypatch.setenv("ONEFOUNDRY_MAX_MODEL_ATTEMPTS", "0")
    assert Settings(db_password="test-only").onefoundry_max_model_attempts == 0
    assert (
        Settings(db_password="test-only", onefoundry_max_model_attempts=3).onefoundry_max_model_attempts == 3
    )
    for field, value in (
        ("onefoundry_max_linked_orders", 9),
        ("onefoundry_max_model_attempts", -1),
        ("onefoundry_max_public_web_calls", 21),
        ("onefoundry_max_context_bytes", 1023),
        ("onefoundry_max_output_tokens", 8193),
        ("onefoundry_max_elapsed_seconds", 29),
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"db_password": "test-only", field: value})


def test_onefoundry_caps_only_guard_manual_mode() -> None:
    assert (
        Settings(db_password="test-only", live_text_max_prompt_bytes=4300).onefoundry_business_mode == "off"
    )
    with pytest.raises(ValidationError, match="OneFoundry context cap"):
        Settings(
            db_password="test-only",
            onefoundry_business_mode="manual",
            live_text_max_prompt_bytes=4300,
            onefoundry_max_context_bytes=16384,
        )
    with pytest.raises(ValidationError, match="OneFoundry output cap"):
        Settings(db_password="test-only", onefoundry_business_mode="manual", live_text_max_output_tokens=128)


def test_unlimited_context_is_explicit_and_never_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEFOUNDRY_MAX_CONTEXT_BYTES", raising=False)
    assert Settings(db_password="test-only").onefoundry_max_context_bytes is None
    monkeypatch.setenv("ONEFOUNDRY_MAX_CONTEXT_BYTES", "unlimited")
    assert Settings(db_password="test-only").onefoundry_max_context_bytes is None
    assert (
        Settings(db_password="test-only", onefoundry_max_context_bytes=16384).onefoundry_max_context_bytes
        == 16384
    )
    assert (
        Settings(db_password="test-only", onefoundry_max_context_bytes=None).onefoundry_max_context_bytes
        is None
    )
    monkeypatch.setenv("ONEFOUNDRY_MAX_CONTEXT_BYTES", "2048")
    assert Settings(db_password="test-only").onefoundry_max_context_bytes == 2048
    assert (
        Settings(db_password="test-only", onefoundry_max_context_bytes=None).onefoundry_max_context_bytes
        is None
    )
    for invalid in ("0", "-1", "", "none", "unexpected"):
        monkeypatch.setenv("ONEFOUNDRY_MAX_CONTEXT_BYTES", invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_onefoundry_event_coordinator_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEFOUNDRY_BUSINESS_MODE", "event")
    monkeypatch.setenv("ONEFOUNDRY_COORDINATOR_POLL_SECONDS", "1")
    settings = Settings(db_password="test-only")
    assert settings.onefoundry_business_mode == "event"
    assert settings.onefoundry_coordinator_poll_seconds == 1
    assert (
        Settings(
            db_password="test-only", onefoundry_coordinator_poll_seconds=4
        ).onefoundry_coordinator_poll_seconds
        == 4
    )
    for changed in (
        {"onefoundry_coordinator_poll_seconds": 0},
        {"onefoundry_coordinator_lease_seconds": 301},
        {"onefoundry_coordinator_lease_seconds": 30, "onefoundry_coordinator_poll_seconds": 30},
        {"onefoundry_coordinator_max_attempts": 11},
        {"onefoundry_coordinator_actor_id": "operator"},
        {"onefoundry_coordinator_actor_id": "  "},
    ):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", **changed)


def test_environment_and_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "environment-secret")
    monkeypatch.setenv("DB_PORT", "5434")
    settings = Settings(db_port=5435)
    assert settings.db_port == 5435
    assert settings.database_url.password == "environment-secret"


@pytest.mark.parametrize("hosts", ["evil.example", "localhost,evil.example", "localhost,localhost", ""])
def test_invalid_allowed_hosts(hosts: str) -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", app_allowed_hosts=hosts)


def test_invalid_port() -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", db_port=0)


def test_invalid_environment_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PORT", "not-a-port")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_codex_connection_settings_defaults_override_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(db_password="test-only")
    assert defaults.codex_device_timeout_seconds == 600
    assert defaults.codex_auth_exchange_timeout_seconds == 60
    assert defaults.codex_catalog_client_version == "0.160.0"
    monkeypatch.setenv("CODEX_DEVICE_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("CODEX_AUTH_EXCHANGE_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("CODEX_CATALOG_CLIENT_VERSION", "0.158.0")
    assert Settings(db_password="test-only").codex_device_timeout_seconds == 300
    assert Settings(db_password="test-only").codex_auth_exchange_timeout_seconds == 90
    assert (
        Settings(
            db_password="test-only", codex_auth_exchange_timeout_seconds=30
        ).codex_auth_exchange_timeout_seconds
        == 30
    )
    assert (
        Settings(db_password="test-only", codex_device_timeout_seconds=120).codex_device_timeout_seconds
        == 120
    )
    assert Settings(db_password="test-only").codex_catalog_client_version == "0.158.0"
    assert (
        Settings(db_password="test-only", codex_catalog_client_version="0.159.0").codex_catalog_client_version
        == "0.159.0"
    )
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", codex_device_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", codex_auth_exchange_timeout_seconds=0)
    monkeypatch.setenv("CODEX_AUTH_EXCHANGE_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")
    monkeypatch.delenv("CODEX_AUTH_EXCHANGE_TIMEOUT_SECONDS")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", codex_catalog_client_version="bad")


def test_xai_client_and_native_text_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_OAUTH_CLIENT_ID", raising=False)
    defaults = Settings(db_password="test-only")
    # Public native OAuth client published in xai-org/grok-build login configuration.
    assert defaults.xai_oauth_client_id == "b1a00492-073a-47ea-816f-4c329264a828"  # gitleaks:allow
    assert defaults.live_text_anthropic_base_url == "https://api.anthropic.com/v1"
    assert defaults.live_text_gemini_base_url == "https://generativelanguage.googleapis.com/v1beta"
    monkeypatch.setenv("XAI_OAUTH_CLIENT_ID", "example-client")
    assert Settings(db_password="test-only").xai_oauth_client_id == "example-client"
    assert Settings(db_password="test-only", xai_oauth_client_id="explicit").xai_oauth_client_id == "explicit"
    monkeypatch.setenv("XAI_OAUTH_CLIENT_ID", "")
    assert Settings(db_password="test-only").xai_oauth_client_id == ""
    for value in ("bad client", "invalid/route", "x" * 129):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", xai_oauth_client_id=value)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", live_text_anthropic_base_url="http://api.anthropic.com/v1")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", live_text_gemini_base_url="https://other.example/v1beta")


@pytest.mark.parametrize(
    ("field", "official"),
    [
        ("xai_oauth_issuer", "https://auth.x.ai"),
        ("grok_account_base_url", "https://cli-chat-proxy.grok.com/v1"),
    ],
)
def test_grok_account_endpoint_defaults_validation_and_precedence(
    monkeypatch: pytest.MonkeyPatch, field: str, official: str
) -> None:
    monkeypatch.delenv(field.upper(), raising=False)
    assert getattr(Settings(db_password="test-only"), field) == official
    monkeypatch.setenv(field.upper(), official)
    assert getattr(Settings(db_password="test-only"), field) == official
    for bad in ("", "http://auth.x.ai", "https://evil.example/v1", official + "?token=test"):
        monkeypatch.setenv(field.upper(), bad)
        with pytest.raises(ValidationError, match="Grok account endpoints"):
            Settings(db_password="test-only")
        explicit = (
            Settings(db_password="test-only", xai_oauth_issuer=official)
            if field == "xai_oauth_issuer"
            else Settings(db_password="test-only", grok_account_base_url=official)
        )
        assert getattr(explicit, field) == official


def test_task_conversation_history_window_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TASK_CONVERSATION_MAX_TURNS", raising=False)
    assert Settings(db_password="test-only").task_conversation_max_turns == 100
    monkeypatch.setenv("TASK_CONVERSATION_MAX_TURNS", "25")
    assert Settings(db_password="test-only").task_conversation_max_turns == 25
    assert Settings(db_password="test-only", task_conversation_max_turns=10).task_conversation_max_turns == 10
    for invalid in (0, 1001):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", task_conversation_max_turns=invalid)
    monkeypatch.setenv("TASK_CONVERSATION_MAX_TURNS", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_remote_channel_settings_precedence_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(db_password="test-only")
    assert defaults.remote_channel_network_enabled is False
    assert defaults.remote_channel_poll_seconds == 5
    assert defaults.remote_identity_challenge_seconds == 600
    assert defaults.discord_max_text_chars == 1900
    assert defaults.discord_send_timeout_seconds == 15
    assert defaults.weixin_api_base_url == "https://ilinkai.weixin.qq.com"
    monkeypatch.setenv("REMOTE_CHANNEL_POLL_SECONDS", "9")
    monkeypatch.setenv("REMOTE_CHANNEL_NETWORK_ENABLED", "true")
    monkeypatch.setenv("WEIXIN_SEND_TIMEOUT_SECONDS", "12")
    assert Settings(db_password="test-only").remote_channel_poll_seconds == 9
    assert Settings(db_password="test-only").remote_channel_network_enabled is True
    assert Settings(db_password="test-only").weixin_send_timeout_seconds == 12
    assert Settings(db_password="test-only", remote_channel_poll_seconds=3).remote_channel_poll_seconds == 3
    assert (
        Settings(db_password="test-only", remote_channel_network_enabled=False).remote_channel_network_enabled
        is False
    )
    for field, value in (
        ("remote_channel_poll_seconds", 0),
        ("remote_identity_challenge_seconds", 59),
        ("discord_max_text_chars", 2001),
        ("discord_send_timeout_seconds", 0),
        ("weixin_api_base_url", "https://other.example"),
        ("weixin_channel_version", "bad"),
        ("weixin_bot_agent", "bad agent"),
        ("weixin_max_response_bytes", 1),
        ("weixin_max_cursor_length", 2049),
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"db_password": "test-only", field: value})
    monkeypatch.setenv("WEIXIN_SEND_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")
    monkeypatch.delenv("WEIXIN_SEND_TIMEOUT_SECONDS")
    monkeypatch.setenv("REMOTE_CHANNEL_NETWORK_ENABLED", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_role_context_item_limit_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROLE_CONTEXT_MAX_ITEMS", raising=False)
    assert Settings(db_password="test-only").role_context_max_items == 5
    monkeypatch.setenv("ROLE_CONTEXT_MAX_ITEMS", "8")
    assert Settings(db_password="test-only").role_context_max_items == 8
    assert Settings(db_password="test-only", role_context_max_items=3).role_context_max_items == 3
    for invalid in (0, 21):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", role_context_max_items=invalid)
    monkeypatch.setenv("ROLE_CONTEXT_MAX_ITEMS", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_automatic_skill_selection_bounds_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILL_AUTO_MAX_COUNT", raising=False)
    monkeypatch.delenv("SKILL_AUTO_MAX_CONTENT_BYTES", raising=False)
    defaults = Settings(db_password="test-only")
    assert (defaults.skill_auto_max_count, defaults.skill_auto_max_content_bytes) == (3, 16000)
    monkeypatch.setenv("SKILL_AUTO_MAX_COUNT", "5")
    monkeypatch.setenv("SKILL_AUTO_MAX_CONTENT_BYTES", "8000")
    inherited = Settings(db_password="test-only")
    assert (inherited.skill_auto_max_count, inherited.skill_auto_max_content_bytes) == (5, 8000)
    explicit = Settings(db_password="test-only", skill_auto_max_count=0, skill_auto_max_content_bytes=0)
    assert (explicit.skill_auto_max_count, explicit.skill_auto_max_content_bytes) == (0, 0)
    for field, invalid in (
        ("skill_auto_max_count", -1),
        ("skill_auto_max_count", 11),
        ("skill_auto_max_content_bytes", -1),
        ("skill_auto_max_content_bytes", 48001),
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"db_password": "test-only", field: invalid})
    monkeypatch.setenv("SKILL_AUTO_MAX_COUNT", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_order_four_tool_limits_validate_and_explicit_values_override_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_PR_READ_APPROVAL_MINUTES", raising=False)
    monkeypatch.delenv("LIVE_TEXT_TOOL_MAX_TURNS", raising=False)
    defaults = Settings(db_password="test-only")
    assert defaults.github_pr_read_approval_minutes == 15
    assert defaults.live_text_tool_max_turns == 3
    monkeypatch.setenv("GITHUB_PR_READ_APPROVAL_MINUTES", "20")
    monkeypatch.setenv("LIVE_TEXT_TOOL_MAX_TURNS", "4")
    configured = Settings(db_password="test-only")
    assert configured.github_pr_read_approval_minutes == 20
    assert configured.live_text_tool_max_turns == 4
    assert Settings(db_password="test-only", live_text_tool_max_turns=2).live_text_tool_max_turns == 2
    assert (
        Settings(db_password="test-only", github_pr_read_approval_minutes=5).github_pr_read_approval_minutes
        == 5
    )
    for field, value in (
        ("github_pr_read_approval_minutes", 0),
        ("github_pr_read_approval_minutes", 61),
        ("live_text_tool_max_turns", 1),
        ("live_text_tool_max_turns", 9),
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"db_password": "test-only", field: value})
    monkeypatch.setenv("LIVE_TEXT_TOOL_MAX_TURNS", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_public_web_settings_are_default_on_and_validate_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLIC_WEB_ENABLED", raising=False)
    settings = Settings(db_password="test-only")
    assert settings.public_web_enabled is True
    assert settings.public_web_search_endpoint == ""
    monkeypatch.setenv("PUBLIC_WEB_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_WEB_SEARCH_ENDPOINT", "https://search.example.org/search")
    configured = Settings(db_password="test-only")
    assert configured.public_web_enabled is True
    assert configured.public_web_search_endpoint == "https://search.example.org/search"
    assert (
        Settings(
            db_password="test-only", public_web_search_endpoint="http://searxng:8080/search"
        ).public_web_search_endpoint
        == "http://searxng:8080/search"
    )
    for endpoint in (
        "http://example.org/search",
        "https://localhost/search",
        "https://user@example.org/search",
    ):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", public_web_search_endpoint=endpoint)
    for field, value in (
        ("public_web_timeout_seconds", 0),
        ("public_web_max_response_bytes", 2000001),
        ("public_web_max_text_chars", 100),
        ("public_web_max_results", 11),
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"db_password": "test-only", field: value})


def test_static_browser_settings_are_default_off_and_require_exact_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BROWSER_STATIC_ENABLED", raising=False)
    defaults = Settings(db_password="test-only")
    assert defaults.browser_static_enabled is False
    assert defaults.browser_static_image_id == ""
    assert defaults.browser_static_claim_timeout_seconds == 240
    assert defaults.browser_static_spool_root == "/runner-spool/browser-static"
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", browser_static_enabled=True)
    image_id = "sha256:" + "a" * 64
    monkeypatch.setenv("BROWSER_STATIC_ENABLED", "true")
    monkeypatch.setenv("BROWSER_STATIC_IMAGE_ID", image_id)
    configured = Settings(db_password="test-only")
    assert configured.browser_static_enabled is True
    assert configured.browser_static_image_id == image_id
    assert Settings(db_password="test-only", browser_static_enabled=False).browser_static_enabled is False
    assert (
        Settings(db_password="test-only", runner_spool_root="/private/spool").browser_static_spool_root
        == "/private/spool/browser-static"
    )
    for invalid in ("onefoundry-browser:latest", "sha256:" + "g" * 64):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", browser_static_image_id=invalid)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", browser_static_claim_timeout_seconds=209)
    assert Settings(db_password="test-only", browser_static_claim_timeout_seconds=210)
    with pytest.raises(ValidationError):
        Settings(
            db_password="test-only",
            browser_static_max_fixture_bytes=262_144,
            browser_static_max_page_bytes=1024,
        )
    monkeypatch.setenv("BROWSER_STATIC_ENABLED", "invalid")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


def test_invalid_public_port_and_actor() -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", app_public_port=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", operator_actor_id="   ")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", operator_actor_id="Ken Operator")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", operator_actor_id="x" * 129)
    assert Settings(db_password="test-only", operator_actor_id="profile:operator_1").operator_actor_id == (
        "profile:operator_1"
    )


def test_public_port_environment_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PUBLIC_PORT", "9876")
    assert Settings(db_password="test-only").app_public_port == 9876
    assert Settings(db_password="test-only", app_public_port=8765).app_public_port == 8765


def test_runner_defaults_environment_and_explicit_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(db_password="test-only")
    assert defaults.runner_spool_root == "/runner-spool"
    assert defaults.runner_limits == {
        "wall_seconds": 30,
        "max_cost_usd": 0,
        "max_log_bytes": 65536,
        "max_diff_bytes": 32768,
    }
    assert defaults.runner_receipt_wait_seconds == 120
    monkeypatch.setenv("RUNNER_SPOOL_ROOT", "/var/lib/onefoundry/fixture-spool")
    monkeypatch.setenv("RUNNER_WALL_SECONDS", "40")
    monkeypatch.setenv("RUNNER_MAX_LOG_BYTES", "8192")
    monkeypatch.setenv("RUNNER_MAX_DIFF_BYTES", "4096")
    monkeypatch.setenv("RUNNER_RECEIPT_WAIT_SECONDS", "70")
    from_env = Settings(db_password="test-only")
    assert from_env.runner_spool_root == "/var/lib/onefoundry/fixture-spool"
    assert from_env.runner_limits["wall_seconds"] == 40
    assert from_env.runner_limits["max_log_bytes"] == 8192
    assert from_env.runner_limits["max_diff_bytes"] == 4096
    assert from_env.runner_receipt_wait_seconds == 70
    explicit = Settings(db_password="test-only", runner_wall_seconds=20, runner_receipt_wait_seconds=60)
    assert explicit.runner_wall_seconds == 20
    assert explicit.runner_receipt_wait_seconds == 60


def test_fake_github_snapshot_root_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(db_password="test-only").github_snapshot_root == "/source-snapshots"
    monkeypatch.setenv("GITHUB_SNAPSHOT_ROOT", "/var/lib/onefoundry/fake-snapshots")
    assert Settings(db_password="test-only").github_snapshot_root == "/var/lib/onefoundry/fake-snapshots"
    assert (
        Settings(
            db_password="test-only", github_snapshot_root="/isolated/source-snapshots"
        ).github_snapshot_root
        == "/isolated/source-snapshots"
    )
    for invalid in ("", "/", "relative/path", "/safe/../escape"):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", github_snapshot_root=invalid)


def test_artifact_workspace_root_default_override_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(db_password="test-only").artifact_workspace_root == "/artifact-workspace"
    monkeypatch.setenv("ARTIFACT_WORKSPACE_ROOT", "/private/artifacts")
    assert Settings(db_password="test-only").artifact_workspace_root == "/private/artifacts"
    assert (
        Settings(db_password="test-only", artifact_workspace_root="/other/artifacts").artifact_workspace_root
        == "/other/artifacts"
    )
    for invalid in ("", "/", "relative/path", "/safe/../escape"):
        with pytest.raises(ValidationError):
            Settings(db_password="test-only", artifact_workspace_root=invalid)


def test_external_code_settings_defaults_validation_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXTERNAL_CODE_ENABLED", raising=False)
    default = Settings(db_password="test-only")
    assert default.external_code_enabled is True
    assert default.external_code_capability_max_age_seconds == 30
    monkeypatch.setenv("EXTERNAL_CODE_ENABLED", "false")
    assert Settings(db_password="test-only").external_code_enabled is False
    assert Settings(db_password="test-only", external_code_enabled=True).external_code_enabled is True
    monkeypatch.setenv("EXTERNAL_CODE_ENABLED", "true")
    monkeypatch.setenv("EXTERNAL_CODE_CAPABILITY_MAX_AGE_SECONDS", "20")
    from_env = Settings(db_password="test-only")
    assert from_env.external_code_enabled is True
    assert from_env.external_code_capability_max_age_seconds == 20
    assert Settings(db_password="test-only", external_code_enabled=False).external_code_enabled is False
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", external_code_capability_max_age_seconds=1)
    for invalid in ("maybe", ""):
        monkeypatch.setenv("EXTERNAL_CODE_ENABLED", invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_team_repository_activation_defaults_validation_and_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_REPOSITORY_READ_ENABLED", raising=False)
    monkeypatch.delenv("LOCAL_SOURCE_ROOT", raising=False)
    defaults = Settings(db_password="test-only")
    assert defaults.team_repository_read_enabled is True
    assert defaults.local_source_root == "/source-roots/workspace"
    monkeypatch.setenv("TEAM_REPOSITORY_READ_ENABLED", "false")
    assert Settings(db_password="test-only", local_source_root="").team_repository_read_enabled is False
    monkeypatch.setenv("TEAM_REPOSITORY_READ_ENABLED", "true")
    with pytest.raises(ValidationError, match="approved LOCAL_SOURCE_ROOT"):
        Settings(db_password="test-only", local_source_root="")
    monkeypatch.setenv("LOCAL_SOURCE_ROOT", "/approved-repositories")
    assert Settings(db_password="test-only").team_repository_read_enabled is True
    assert (
        Settings(db_password="test-only", team_repository_read_enabled=False).team_repository_read_enabled
        is False
    )
    for invalid in ("maybe", ""):
        monkeypatch.setenv("TEAM_REPOSITORY_READ_ENABLED", invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_offline_probe_setting_requires_explicit_enablement(monkeypatch: pytest.MonkeyPatch) -> None:
    default = Settings(db_password="test-only")
    assert default.offline_source_probe_enabled is False
    assert default.offline_source_probe_capability_max_age_seconds == 30
    monkeypatch.setenv("OFFLINE_SOURCE_PROBE_ENABLED", "true")
    monkeypatch.setenv("OFFLINE_SOURCE_PROBE_CAPABILITY_MAX_AGE_SECONDS", "20")
    configured = Settings(db_password="test-only")
    assert configured.offline_source_probe_enabled is True
    assert configured.offline_source_probe_capability_max_age_seconds == 20
    assert (
        Settings(db_password="test-only", offline_source_probe_enabled=False).offline_source_probe_enabled
        is False
    )
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", offline_source_probe_capability_max_age_seconds=1)
    monkeypatch.setenv("OFFLINE_SOURCE_PROBE_ENABLED", "maybe")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")


@pytest.mark.parametrize(
    "field,value",
    [
        ("runner_spool_root", "relative/path"),
        ("runner_spool_root", "/var/lib/../spool"),
        ("runner_wall_seconds", 0),
        ("runner_wall_seconds", 61),
        ("runner_max_log_bytes", 511),
        ("runner_max_log_bytes", 65537),
        ("runner_max_diff_bytes", 511),
        ("runner_max_diff_bytes", 65537),
        ("runner_receipt_wait_seconds", 0),
        ("runner_receipt_wait_seconds", 601),
    ],
)
def test_invalid_runner_setting(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"db_password": "test-only", field: value})


def test_runner_wait_must_cover_wall() -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", runner_wall_seconds=60, runner_receipt_wait_seconds=59)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", runner_wall_seconds=60, runner_receipt_wait_seconds=89)
    assert (
        Settings(
            db_password="test-only", runner_wall_seconds=60, runner_receipt_wait_seconds=90
        ).runner_receipt_wait_seconds
        == 90
    )


def test_infra_settings_defaults_environment_and_explicit_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(db_password="test-only")
    assert defaults.infra_snapshot_path == "/infra/status.json"
    assert defaults.infra_snapshot_max_age_seconds == 20
    monkeypatch.setenv("INFRA_SNAPSHOT_PATH", "/observations/stack.json")
    monkeypatch.setenv("INFRA_SNAPSHOT_MAX_AGE_SECONDS", "45")
    from_env = Settings(db_password="test-only")
    assert from_env.infra_snapshot_path == "/observations/stack.json"
    assert from_env.infra_snapshot_max_age_seconds == 45
    explicit = Settings(db_password="test-only", infra_snapshot_max_age_seconds=15)
    assert explicit.infra_snapshot_max_age_seconds == 15
    assert explicit.infra_snapshot_path == "/observations/stack.json"


@pytest.mark.parametrize("path", ["", "status.json", "/", "/var/../status.json", "/infra/"])
def test_invalid_infra_path(path: str) -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", infra_snapshot_path=path)


@pytest.mark.parametrize("age", [0, 1, 3601, "invalid"])
def test_invalid_infra_age(age: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"db_password": "test-only", "infra_snapshot_max_age_seconds": age})


def test_manual_gateway_does_not_invent_runner_configuration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        gateway = ToolGateway(engine, "operator")
        with pytest.raises(Unavailable, match="synthetic runner configuration unavailable"):
            gateway._code_store()
    finally:
        engine.dispose()


def test_bootstrap_role_rejected_before_database_connection() -> None:
    bootstrap = Settings(db_user="onefoundry", db_password="bootstrap-test")
    runtime = RuntimeRoleSettings(app_db_user="onefoundry", app_db_password="runtime-test")
    with patch("onefoundry.db_roles.connect") as connect:
        with pytest.raises(ValueError, match="must differ"):
            provision_runtime_role(bootstrap, runtime)
    connect.assert_not_called()


@pytest.mark.parametrize("name", ["", "BadCase", "bad-name", "bad name", "pg_reserved", "x" * 64])
def test_invalid_runtime_role_names(name: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeRoleSettings(app_db_user=name, app_db_password="runtime-test")


@pytest.mark.parametrize("password", ["", "   "])
def test_blank_runtime_password(password: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeRoleSettings(app_db_user="onefoundry_app", app_db_password=password)


def test_team_capacity_and_no_progress_settings_precedence_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, default, field in (
        ("TEAM_MAX_ASSIGNMENTS_PER_ORDER", 8, "team_max_assignments_per_order"),
        ("TEAM_MAX_CHIEF_DECISIONS_PER_ORDER", 6, "team_max_chief_decisions_per_order"),
    ):
        monkeypatch.delenv(name, raising=False)
        assert getattr(Settings(db_password="test-only"), field) == default
        monkeypatch.setenv(name, "4")
        assert getattr(Settings(db_password="test-only"), field) == 4
        override = (
            Settings(db_password="test-only", team_max_assignments_per_order=2)
            if field == "team_max_assignments_per_order"
            else Settings(db_password="test-only", team_max_chief_decisions_per_order=2)
        )
        assert getattr(override, field) == 2
        for value in ("0", "51", "invalid"):
            monkeypatch.setenv(name, value)
            with pytest.raises(ValidationError):
                Settings(db_password="test-only")
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("explicit", [None, 2, 8, 30])
def test_sdk_call_limit_explicit_setting_overrides_environment(
    monkeypatch: pytest.MonkeyPatch, explicit: int | None
) -> None:
    monkeypatch.setenv("TEAM_MAX_MODEL_CALLS_PER_TURN", "3")
    assert (
        Settings(
            db_password="test-only", team_max_model_calls_per_turn=explicit
        ).team_max_model_calls_per_turn
        == explicit
    )


def test_sdk_call_limit_defaults_to_unlimited_and_validates_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_MAX_MODEL_CALLS_PER_TURN", raising=False)
    assert Settings(db_password="test-only").team_max_model_calls_per_turn is None
    monkeypatch.setenv("TEAM_MAX_MODEL_CALLS_PER_TURN", "unlimited")
    assert Settings(db_password="test-only").team_max_model_calls_per_turn is None
    monkeypatch.setenv("TEAM_MAX_MODEL_CALLS_PER_TURN", "8")
    assert Settings(db_password="test-only").team_max_model_calls_per_turn == 8
    for invalid in ("0", "1", "31", "", "none", "unexpected"):
        monkeypatch.setenv("TEAM_MAX_MODEL_CALLS_PER_TURN", invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_public_web_size_limits_match_transport_and_follow_settings_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUBLIC_WEB_MAX_RESPONSE_BYTES", raising=False)
    monkeypatch.delenv("PUBLIC_WEB_MAX_TEXT_CHARS", raising=False)
    defaults = Settings(db_password="test-only")
    assert defaults.public_web_max_response_bytes == 65536
    assert defaults.public_web_max_text_chars == 4000

    monkeypatch.setenv("PUBLIC_WEB_MAX_RESPONSE_BYTES", "1048576")
    monkeypatch.setenv("PUBLIC_WEB_MAX_TEXT_CHARS", "16000")
    configured = Settings(db_password="test-only")
    assert configured.public_web_max_response_bytes == 1048576
    assert configured.public_web_max_text_chars == 16000
    explicit = Settings(
        db_password="test-only",
        public_web_max_response_bytes=2000000,
        public_web_max_text_chars=8000,
    )
    assert explicit.public_web_max_response_bytes == 2000000
    assert explicit.public_web_max_text_chars == 8000

    for name, invalid in (
        ("PUBLIC_WEB_MAX_RESPONSE_BYTES", "2000001"),
        ("PUBLIC_WEB_MAX_RESPONSE_BYTES", "1023"),
        ("PUBLIC_WEB_MAX_TEXT_CHARS", "16001"),
        ("PUBLIC_WEB_MAX_TEXT_CHARS", "255"),
    ):
        monkeypatch.setenv(name, invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")
        monkeypatch.setenv(name, "1048576" if name.endswith("BYTES") else "16000")
