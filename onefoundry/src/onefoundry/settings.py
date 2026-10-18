"""Validated runtime settings for the local OneFoundry service."""

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from onefoundry.repository_code_contract import RepositoryCodePolicy

MAX_TASK_CONVERSATION_TURNS = 1000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="forbid")

    extension_allowed_origins: list[str] = Field(default_factory=list, max_length=64)
    extension_timeout_seconds: int = Field(default=10, ge=1, le=120)
    extension_max_input_bytes: int = Field(default=65536, ge=1024, le=1048576)
    extension_max_response_bytes: int = Field(default=262144, ge=1024, le=4194304)
    extension_webhook_max_bytes: int = Field(default=65536, ge=1024, le=1048576)
    extension_webhook_window_seconds: int = Field(default=300, ge=30, le=900)
    extension_worker_image: str = Field(default="", pattern=r"^(?:sha256:[0-9a-f]{64})?$")
    extension_worker_memory_mb: int = Field(default=256, ge=128, le=1024)
    extension_worker_processes: int = Field(default=32, ge=8, le=64)
    extension_worker_cpus: int = Field(default=1, ge=1, le=2)
    extension_worker_max_output_bytes: int = Field(default=65536, ge=1024, le=65536)
    extension_worker_poll_seconds: float = Field(default=0.25, ge=0.1, le=10)
    extension_worker_heartbeat_seconds: int = Field(default=30, ge=5, le=300)
    extension_worker_queue_seconds: int = Field(default=60, ge=5, le=600)

    @field_validator("extension_allowed_origins")
    @classmethod
    def valid_extension_origins(cls, values: list[str]) -> list[str]:
        from onefoundry.gateway_extension_transport import canonical_origin

        canonical = []
        for value in values:
            try:
                origin = canonical_origin(value)
            except Exception:
                raise ValueError("extension origins must be explicit HTTP(S) origins") from None
            if value != origin:
                raise ValueError("extension origins must be canonical origins without paths")
            canonical.append(origin)
        if len(set(canonical)) != len(canonical):
            raise ValueError("extension origins must be distinct")
        return canonical

    memory_search_mode: Literal["lexical", "hybrid"] = "lexical"
    memory_embedding_runtime_path: str = ""
    memory_embedding_runtime_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    memory_embedding_model_path: str = ""
    memory_embedding_model_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    memory_embedding_model: str = Field(default="", max_length=240)
    memory_embedding_context_length: int = Field(default=2048, ge=256, le=2048)
    memory_embedding_dimensions: int = Field(default=0, ge=0, le=4000)
    memory_embedding_document_prefix: str = Field(default="", max_length=64)
    memory_embedding_query_prefix: str = Field(default="", max_length=64)
    memory_embedding_timeout_seconds: int = Field(default=5, ge=1, le=30)
    memory_embedding_max_response_bytes: int = Field(default=1048576, ge=4096, le=2097152)
    memory_embedding_min_similarity: float = Field(default=0.5, ge=-1, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_memory_embeddings(self) -> "Settings":
        from pathlib import Path

        for path in (self.memory_embedding_runtime_path, self.memory_embedding_model_path):
            if path and not Path(path).is_absolute():
                raise ValueError("native embedding assets require absolute paths")
        if self.memory_search_mode == "hybrid" and (
            not self.memory_embedding_runtime_path
            or not self.memory_embedding_runtime_sha256
            or not self.memory_embedding_model_path
            or not self.memory_embedding_model_sha256
            or not self.memory_embedding_model.strip()
            or self.memory_embedding_dimensions == 0
            or not self.memory_embedding_document_prefix
            or not self.memory_embedding_query_prefix
        ):
            raise ValueError(
                "hybrid memory requires pinned native assets, model, dimensions and task prefixes"
            )
        return self

    onefoundry_business_mode: Literal["off", "records", "manual", "event", "inbox"] = "off"
    onefoundry_max_linked_orders: int = Field(default=2, ge=1, le=8)
    onefoundry_max_model_attempts: int = Field(default=2, ge=0, le=8)
    onefoundry_max_public_web_calls: int = Field(default=0, ge=0, le=20)
    onefoundry_max_context_bytes: int | None = Field(default=None, ge=1024, le=32000)
    onefoundry_max_output_tokens: int = Field(default=512, ge=1, le=8192)
    onefoundry_max_elapsed_seconds: int = Field(default=600, ge=30, le=3600)
    onefoundry_coordinator_poll_seconds: int = Field(default=5, ge=1, le=30)
    onefoundry_coordinator_lease_seconds: int = Field(default=60, ge=30, le=300)
    onefoundry_coordinator_max_attempts: int = Field(default=3, ge=1, le=10)
    onefoundry_coordinator_actor_id: str = Field(default="onefoundry-coordinator", min_length=1)
    coordination_mode: Literal["off", "draft_only", "auto"] = "off"
    coordination_max_chain_depth: int = Field(default=10, ge=1, le=30)
    coordination_max_orders_per_day: int = Field(default=50, ge=1, le=1000)
    coordination_timezone: str = Field(default="UTC", min_length=1, max_length=64)
    coordination_launcher_actor_id: str = Field(default="coordination-launcher", min_length=1, max_length=80)
    coordination_max_auto_starts_per_day: int = Field(default=20, ge=1, le=500)
    connector_http_timeout_seconds: int = Field(default=10, ge=2, le=30)
    connector_google_redirect_uri: str = Field(default="", max_length=200)
    zoho_oauth_pending_timeout_seconds: int = Field(default=900, ge=60, le=1800)
    zoho_oauth_authority_hosts: tuple[str, ...] = ("mcp.zoho.com", "accounts.zoho.com")
    document_poll_seconds: int = Field(default=300, ge=60, le=86400)
    document_max_bytes: int = Field(default=4_000_000, ge=1024, le=4_000_000)
    document_max_files_per_poll: int = Field(default=10, ge=1, le=50)
    document_search_max_pages: int = Field(default=5, ge=1, le=20)
    document_search_max_results: int = Field(default=20, ge=1, le=100)
    document_max_range_cells: int = Field(default=1000, ge=1, le=10000)
    mail_poll_seconds: int = Field(default=60, ge=15, le=3600)
    mail_idle_enabled: bool = True
    mail_idle_wait_seconds: int = Field(default=60, ge=5, le=120)
    mail_timeout_seconds: int = Field(default=20, ge=2, le=60)
    mail_max_message_bytes: int = Field(default=262144, ge=4096, le=1048576)
    mail_max_fetch_per_poll: int = Field(default=20, ge=1, le=100)
    mail_max_snippet_chars: int = Field(default=1000, ge=100, le=2000)
    smtp_timeout_seconds: int = Field(default=15, ge=2, le=60)

    db_host: str = Field(default="db", min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(default="onefoundry", min_length=1)
    db_user: str = Field(default="onefoundry", min_length=1)
    db_password: str = Field(default="", min_length=1, repr=False)
    app_allowed_hosts: str = "localhost,127.0.0.1"
    app_public_port: int = Field(default=8765, ge=1, le=65535)
    operator_actor_id: str = Field(default="operator", min_length=1)
    runner_spool_root: str = Field(default="/runner-spool", min_length=1)
    github_snapshot_root: str = Field(default="/source-snapshots", min_length=2)
    runner_wall_seconds: int = Field(default=30, ge=1, le=60)
    runner_max_log_bytes: int = Field(default=65536, ge=512, le=65536)
    runner_max_diff_bytes: int = Field(default=32768, ge=512, le=65536)
    runner_receipt_wait_seconds: int = Field(default=120, ge=1, le=600)
    external_code_enabled: bool = True
    repository_code_policy: RepositoryCodePolicy = Field(default_factory=RepositoryCodePolicy)
    external_code_capability_max_age_seconds: int = Field(default=30, ge=2, le=300)
    offline_source_probe_enabled: bool = False
    offline_source_probe_capability_max_age_seconds: int = Field(default=30, ge=2, le=300)
    infra_snapshot_path: str = Field(default="/infra/status.json", min_length=2)
    infra_snapshot_max_age_seconds: int = Field(default=20, ge=2, le=3600)
    sales_mailpit_host: str = "mailpit"
    sales_mailpit_smtp_port: int = Field(default=1025, ge=1, le=65535)
    sales_mailpit_http_port: int = Field(default=8025, ge=1, le=65535)
    model_secret_root: str = Field(default="/model-secrets", min_length=2)
    artifact_workspace_root: str = Field(default="/artifact-workspace", min_length=2)
    video_edit_enabled: bool = False
    video_edit_asset_root: str = "/video-edit-assets"
    video_edit_image: str = Field(default="", pattern=r"^(?:sha256:[0-9a-f]{64})?$")
    video_edit_docker_binary: str = "/usr/bin/docker"
    video_edit_docker_socket: str = "/var/run/docker.sock"
    video_edit_wall_seconds: int = Field(default=120, ge=1, le=600)
    video_edit_memory_mb: int = Field(default=512, ge=128, le=2048)
    video_edit_cpus: int = Field(default=1, ge=1, le=4)
    video_edit_pids: int = Field(default=64, ge=16, le=128)
    video_edit_max_scenes: int = Field(default=12, ge=1, le=32)
    video_edit_max_captions: int = Field(default=64, ge=0, le=128)
    video_edit_max_jobs_per_order: int = Field(default=3, ge=1, le=16)
    video_edit_max_attempts: int = Field(default=2, ge=1, le=3)

    @model_validator(mode="after")
    def valid_video_edit(self) -> "Settings":
        root = Path(self.video_edit_asset_root)
        if not root.is_absolute() or root == Path("/") or ".." in root.parts:
            raise ValueError("video editing requires an absolute dedicated asset root")
        if self.video_edit_enabled and not self.video_edit_image:
            raise ValueError("video editing requires a pinned renderer image digest")
        for value in (self.video_edit_docker_binary, self.video_edit_docker_socket):
            path = Path(value)
            if not path.is_absolute() or path == Path("/") or ".." in path.parts or "\x00" in value:
                raise ValueError("video editing requires absolute local Docker binary/socket paths")
        return self

    media_capture_enabled: bool = True
    media_max_bytes: int = Field(default=33_554_432, ge=1024, le=67_108_864)
    media_max_duration_seconds: int = Field(default=120, ge=1, le=300)
    media_max_width: int = Field(default=1280, ge=320, le=1920)
    media_max_height: int = Field(default=720, ge=240, le=1080)
    media_frame_rate: int = Field(default=24, ge=1, le=30)
    input_file_root: str = Field(default="/input-files", min_length=2)
    input_file_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    input_file_max_project_bytes: int = Field(default=200 * 1024 * 1024, ge=1, le=2 * 1024 * 1024 * 1024)
    input_file_max_project_count: int = Field(default=30, ge=1, le=100)
    input_file_max_link_count: int = Field(default=8, ge=1, le=32)
    input_file_max_text_bytes: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)
    input_file_max_visual_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    input_file_max_visual_count: int = Field(default=2, ge=1, le=8)
    local_source_root: str = "/source-roots/workspace"
    local_source_scan_max_entries: int = Field(default=25, ge=1, le=100)
    local_source_scan_seconds: int = Field(default=10, ge=1, le=30)
    local_source_git_timeout_seconds: int = Field(default=3, ge=1, le=10)
    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: int = Field(default=5, ge=1, le=15)
    github_max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    github_pr_read_approval_minutes: int = Field(default=15, ge=1, le=60)
    model_openai_list_url: str = "https://api.openai.com/v1/models"
    model_anthropic_list_url: str = "https://api.anthropic.com/v1/models"
    model_gemini_list_url: str = "https://generativelanguage.googleapis.com/v1beta/models"
    model_grok_list_url: str = "https://api.x.ai/v1/models"
    codex_device_timeout_seconds: int = Field(default=600, ge=60, le=1800)
    codex_auth_exchange_timeout_seconds: int = Field(default=60, ge=10, le=120)
    codex_catalog_client_version: str = "0.160.0"
    # Public native client published by xai-org/grok-build, not a credential.
    xai_oauth_client_id: str = "b1a00492-073a-47ea-816f-4c329264a828"
    xai_oauth_issuer: str = "https://auth.x.ai"
    grok_account_base_url: str = "https://cli-chat-proxy.grok.com/v1"
    live_text_openai_base_url: str = "https://api.openai.com/v1"
    live_text_anthropic_base_url: str = "https://api.anthropic.com/v1"
    live_text_gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    live_text_grok_base_url: str = "https://api.x.ai/v1"
    model_discovery_timeout_seconds: int = Field(default=5, ge=1, le=15)
    model_discovery_max_pages: int = Field(default=5, ge=1, le=10)
    model_discovery_max_response_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000)
    model_discovery_max_models: int = Field(default=500, ge=1, le=1000)
    live_text_max_prompt_bytes: int = Field(default=96000, ge=1024, le=1000000)
    agent_automation_context_max_items: int = Field(default=8, ge=1, le=50)
    agent_automation_goal_max_chars: int = Field(default=500, ge=100, le=4000)
    skill_auto_max_count: int = Field(default=3, ge=0, le=10)
    skill_auto_max_content_bytes: int = Field(default=16000, ge=0, le=48000)
    live_text_max_output_tokens: int = Field(default=512, ge=1, le=8192)
    live_text_max_output_bytes: int = Field(default=16000, ge=1024, le=16000)
    live_text_timeout_seconds: int = Field(default=30, ge=1, le=120)
    task_chat_timeout_seconds: int = Field(default=120, ge=1, le=600)

    @field_validator("connector_google_redirect_uri")
    @classmethod
    def loopback_redirect(cls, value: str) -> str:
        if value and not re.fullmatch(
            r"http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?/[A-Za-z0-9/_-]*", value
        ):
            raise ValueError("Google redirect must be a loopback http URL such as http://127.0.0.1:8765/")
        return value

    @field_validator("zoho_oauth_authority_hosts")
    @classmethod
    def zoho_authorities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not value
            or len(set(value)) != len(value)
            or any(not re.fullmatch(r"(?:[a-z0-9-]+\.)+(?:zoho\.com|zohomcp\.com)", host) for host in value)
        ):
            raise ValueError("Zoho OAuth authorities must be distinct official Zoho hostnames")
        return value

    @field_validator("coordination_timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if value == "UTC":
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("coordination timezone must be an IANA name such as America/New_York") from None
        return value

    @model_validator(mode="after")
    def validate_onefoundry_caps(self) -> "Settings":
        if not self.onefoundry_coordinator_actor_id.strip():
            raise ValueError("OneFoundry coordinator actor must not be blank")
        if self.onefoundry_coordinator_lease_seconds <= self.onefoundry_coordinator_poll_seconds:
            raise ValueError("OneFoundry coordinator lease must exceed poll interval")
        if self.onefoundry_coordinator_actor_id == self.operator_actor_id:
            raise ValueError("OneFoundry coordinator actor must differ from operator actor")
        if self.coordination_launcher_actor_id.strip() in {self.operator_actor_id, ""}:
            raise ValueError("coordination launcher actor must be non-blank and differ from operator actor")
        if self.onefoundry_business_mode not in {"manual", "event", "inbox"}:
            return self
        if (
            self.onefoundry_max_context_bytes is not None
            and self.onefoundry_max_context_bytes > self.live_text_max_prompt_bytes
        ):
            raise ValueError("OneFoundry context cap exceeds the live text prompt cap")
        if self.onefoundry_max_output_tokens > self.live_text_max_output_tokens:
            raise ValueError("OneFoundry output cap exceeds the live text output cap")
        return self

    @field_validator("onefoundry_max_context_bytes", "team_max_model_calls_per_turn", mode="before")
    @classmethod
    def unlimited_team_limit(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() == "unlimited":
            return None
        return value

    @model_validator(mode="after")
    def validate_team_turn_bounds(self) -> "Settings":
        if self.team_turn_timeout_seconds >= self.onefoundry_max_elapsed_seconds:
            raise ValueError("team turn timeout must be shorter than the abandoned-turn bound")
        return self

    live_text_tool_max_turns: int = Field(default=3, ge=2, le=8)
    role_context_max_items: int = Field(default=5, ge=1, le=20)
    public_web_enabled: bool = True
    public_web_search_endpoint: str = ""
    public_web_timeout_seconds: int = Field(default=8, ge=1, le=15)
    public_web_max_response_bytes: int = Field(default=65536, ge=1024, le=2_000_000)
    public_web_max_text_chars: int = Field(default=4000, ge=256, le=16000)
    public_web_max_results: int = Field(default=5, ge=1, le=10)
    browser_static_enabled: bool = False
    browser_static_image_id: str = ""
    browser_static_claim_timeout_seconds: int = Field(default=240, ge=15, le=3600)
    browser_static_max_url_chars: int = Field(default=2048, ge=1, le=2048)
    browser_static_max_viewport_width: int = Field(default=1920, ge=1, le=3840)
    browser_static_max_viewport_height: int = Field(default=1200, ge=1, le=2160)
    browser_static_max_page_bytes: int = Field(default=262_144, ge=1024, le=262_144)
    browser_static_max_fixture_bytes: int = Field(default=262_144, ge=1024, le=262_144)
    browser_static_max_timeout_seconds: int = Field(default=30, ge=1, le=30)
    browser_static_max_lifetime_seconds: int = Field(default=300, ge=1, le=3600)
    # Compatibility name: recent context/history page size, never a lifetime cutoff.
    task_conversation_max_turns: int = Field(default=100, ge=1, le=MAX_TASK_CONVERSATION_TURNS)
    team_worker_enabled: bool = True
    team_repository_read_enabled: bool = True

    @model_validator(mode="after")
    def validate_team_repository_source(self) -> "Settings":
        if self.team_repository_read_enabled and not self.local_source_root:
            raise ValueError("TEAM_REPOSITORY_READ_ENABLED requires an approved LOCAL_SOURCE_ROOT")
        return self

    team_max_delegation_depth: int = Field(default=2, ge=1, le=5)
    # Count unfinished assignments; terminal history does not consume capacity.
    team_max_assignments_per_order: int = Field(default=8, ge=1, le=50)
    team_max_plan_revisions: int = Field(default=3, ge=1, le=20)
    # Automatic decisions since novel confirmed evidence or an operator checkpoint.
    team_max_chief_decisions_per_order: int = Field(default=6, ge=1, le=50)
    team_max_model_calls_per_turn: int | None = Field(default=None, ge=2, le=30)
    team_turn_timeout_seconds: int = Field(default=300, ge=30, le=600)
    team_max_output_tokens: int = Field(default=4096, ge=256, le=32768)
    team_retry_delay_seconds: int = Field(default=30, ge=5, le=600)
    remote_channel_network_enabled: bool = False
    remote_channel_poll_seconds: int = Field(default=5, ge=1, le=60)
    remote_identity_challenge_seconds: int = Field(default=600, ge=60, le=3600)
    discord_max_text_chars: int = Field(default=1900, ge=1, le=2000)
    discord_send_timeout_seconds: int = Field(default=15, ge=1, le=60)
    weixin_api_base_url: str = "https://ilinkai.weixin.qq.com"
    weixin_channel_version: str = "2.4.9"
    weixin_app_client_version: int = Field(default=132105, ge=1, le=16777215)
    weixin_bot_agent: str = "OneFoundry/0.1.0"
    weixin_qr_request_timeout_seconds: float = Field(default=5, ge=1, le=60)
    weixin_qr_poll_timeout_seconds: float = Field(default=35, ge=1, le=60)
    weixin_poll_timeout_seconds: int = Field(default=35, ge=1, le=40)
    weixin_max_poll_timeout_seconds: int = Field(default=40, ge=1, le=60)
    weixin_send_timeout_seconds: float = Field(default=15, ge=1, le=60)
    weixin_update_retries: int = Field(default=3, ge=1, le=5)
    weixin_retry_backoff_seconds: float = Field(default=0.5, ge=0.01, le=10)
    weixin_max_response_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    weixin_max_cursor_length: int = Field(default=2048, ge=1, le=2048)
    weixin_max_text_length: int = Field(default=8192, ge=1, le=8192)

    @model_validator(mode="after")
    def validate_runner_wait(self) -> "Settings":
        if self.runner_receipt_wait_seconds < self.runner_wall_seconds + 30:
            raise ValueError("RUNNER_RECEIPT_WAIT_SECONDS must exceed RUNNER_WALL_SECONDS by 30")
        return self

    @model_validator(mode="after")
    def validate_input_limits(self) -> "Settings":
        if self.input_file_max_project_bytes < self.input_file_max_bytes:
            raise ValueError("INPUT_FILE_MAX_PROJECT_BYTES must allow one maximum-size file")
        if self.input_file_max_visual_bytes > self.input_file_max_project_bytes:
            raise ValueError("INPUT_FILE_MAX_VISUAL_BYTES exceeds the project input limit")
        return self

    @model_validator(mode="after")
    def validate_static_browser(self) -> "Settings":
        if (
            self.browser_static_image_id
            and re.fullmatch(r"sha256:[0-9a-f]{64}", self.browser_static_image_id) is None
        ):
            raise ValueError("BROWSER_STATIC_IMAGE_ID must be an exact Docker image ID")
        if self.browser_static_enabled and not self.browser_static_image_id:
            raise ValueError("BROWSER_STATIC_IMAGE_ID is required when static browser is enabled")
        # The claim covers image inspection, container creation/start/removal,
        # and durable receipt publication in addition to the page timeout.
        if self.browser_static_claim_timeout_seconds < self.browser_static_max_timeout_seconds + 180:
            raise ValueError("BROWSER_STATIC_CLAIM_TIMEOUT_SECONDS must exceed browser timeout by 180")
        if self.browser_static_max_fixture_bytes > self.browser_static_max_page_bytes:
            raise ValueError("BROWSER_STATIC_MAX_FIXTURE_BYTES must not exceed page byte limit")
        return self

    @property
    def browser_static_spool_root(self) -> str:
        return str(Path(self.runner_spool_root) / "browser-static")

    @property
    def runner_limits(self) -> dict[str, int]:
        return {
            "wall_seconds": self.runner_wall_seconds,
            "max_cost_usd": 0,
            "max_log_bytes": self.runner_max_log_bytes,
            "max_diff_bytes": self.runner_max_diff_bytes,
        }

    @field_validator("db_password", "operator_actor_id")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("operator_actor_id")
    @classmethod
    def validate_runner_actor(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", value) is None:
            raise ValueError("OPERATOR_ACTOR_ID must be a runner-compatible identifier")
        return value

    @field_validator("app_allowed_hosts")
    @classmethod
    def validate_hosts(cls, value: str) -> str:
        hosts = value.split(",")
        if not hosts or any(host not in {"localhost", "127.0.0.1"} for host in hosts):
            raise ValueError("APP_ALLOWED_HOSTS may contain only localhost and 127.0.0.1")
        if len(set(hosts)) != len(hosts):
            raise ValueError("APP_ALLOWED_HOSTS contains duplicate hosts")
        return value

    @field_validator("runner_spool_root")
    @classmethod
    def validate_spool_root(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or ".." in value.split("/"):
            raise ValueError("RUNNER_SPOOL_ROOT must be an absolute dedicated directory")
        return value.rstrip("/")

    @field_validator("github_snapshot_root")
    @classmethod
    def validate_github_snapshot_root(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or ".." in value.split("/"):
            raise ValueError("GITHUB_SNAPSHOT_ROOT must be an absolute dedicated directory")
        return value.rstrip("/")

    @field_validator("infra_snapshot_path")
    @classmethod
    def validate_infra_path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or ".." in value.split("/") or value.endswith("/"):
            raise ValueError("INFRA_SNAPSHOT_PATH must be an absolute file path without traversal")
        return value

    @field_validator("model_secret_root")
    @classmethod
    def validate_model_secret_root(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or ".." in value.split("/"):
            raise ValueError("MODEL_SECRET_ROOT must be an absolute dedicated directory")
        return value.rstrip("/")

    @field_validator("artifact_workspace_root")
    @classmethod
    def validate_artifact_workspace_root(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or ".." in value.split("/"):
            raise ValueError("ARTIFACT_WORKSPACE_ROOT must be an absolute dedicated directory")
        return value.rstrip("/")

    @field_validator("input_file_root")
    @classmethod
    def validate_input_file_root(cls, value: str) -> str:
        if not value.startswith("/") or value == "/" or ".." in value.split("/"):
            raise ValueError("INPUT_FILE_ROOT must be an absolute dedicated directory")
        return value.rstrip("/")

    @field_validator("local_source_root")
    @classmethod
    def validate_local_source_root(cls, value: str) -> str:
        if value and (not value.startswith("/") or value == "/" or ".." in value.split("/")):
            raise ValueError("LOCAL_SOURCE_ROOT must be an absolute dedicated directory or empty")
        return value.rstrip("/")

    @field_validator("github_api_url")
    @classmethod
    def validate_github_api_url(cls, value: str) -> str:
        if value != "https://api.github.com":
            raise ValueError("GITHUB_API_URL must be the official HTTPS API origin")
        return value

    @field_validator("public_web_search_endpoint")
    @classmethod
    def validate_public_web_search_endpoint(cls, value: str) -> str:
        if not value:
            return value
        if value == "http://searxng:8080/search":
            return value
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/search"
            or parsed.query
            or parsed.fragment
            or re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", hostname) is None
        ):
            raise ValueError("PUBLIC_WEB_SEARCH_ENDPOINT must be a public HTTPS /search endpoint")
        return value

    @model_validator(mode="after")
    def validate_model_endpoints(self) -> "Settings":
        expected = (
            "https://api.openai.com/v1/models",
            "https://api.anthropic.com/v1/models",
            "https://generativelanguage.googleapis.com/v1beta/models",
            "https://api.x.ai/v1/models",
        )
        actual = (
            self.model_openai_list_url,
            self.model_anthropic_list_url,
            self.model_gemini_list_url,
            self.model_grok_list_url,
        )
        if actual != expected:
            raise ValueError("model list endpoints must be the official HTTPS URLs")
        if (
            self.xai_oauth_issuer != "https://auth.x.ai"
            or self.grok_account_base_url != "https://cli-chat-proxy.grok.com/v1"
        ):
            raise ValueError("Grok account endpoints must be the official HTTPS URLs")
        if (
            self.live_text_openai_base_url != "https://api.openai.com/v1"
            or self.live_text_anthropic_base_url != "https://api.anthropic.com/v1"
            or self.live_text_gemini_base_url != "https://generativelanguage.googleapis.com/v1beta"
            or self.live_text_grok_base_url != "https://api.x.ai/v1"
        ):
            raise ValueError("live text endpoints must be the official HTTPS URLs")
        return self

    @field_validator("weixin_api_base_url")
    @classmethod
    def validate_weixin_api_base_url(cls, value: str) -> str:
        if value != "https://ilinkai.weixin.qq.com":
            raise ValueError("WEIXIN_API_BASE_URL must be the official HTTPS iLink origin")
        return value

    @field_validator("weixin_channel_version")
    @classmethod
    def validate_weixin_channel_version(cls, value: str) -> str:
        if re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", value) is None:
            raise ValueError("WEIXIN_CHANNEL_VERSION must be a semantic version")
        return value

    @field_validator("weixin_bot_agent")
    @classmethod
    def validate_weixin_bot_agent(cls, value: str) -> str:
        if len(value) > 256 or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("WEIXIN_BOT_AGENT must be a product/version identifier")
        return value

    @field_validator("codex_catalog_client_version")
    @classmethod
    def validate_codex_catalog_version(cls, value: str) -> str:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None:
            raise ValueError("CODEX_CATALOG_CLIENT_VERSION must be a semantic version")
        return value

    @field_validator("xai_oauth_client_id")
    @classmethod
    def validate_xai_client_id(cls, value: str) -> str:
        if value and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None:
            raise ValueError("XAI_OAUTH_CLIENT_ID must be a valid OAuth client identifier")
        return value

    @field_validator("sales_mailpit_host")
    @classmethod
    def validate_mailpit_host(cls, value: str) -> str:
        if value != "mailpit":
            raise ValueError("SALES_MAILPIT_HOST must be the internal Compose service mailpit")
        return value

    @property
    def allowed_hosts(self) -> list[str]:
        return self.app_allowed_hosts.split(",")

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
