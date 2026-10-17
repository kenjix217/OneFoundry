"""Operator contracts for coordination rules, controls, and signal readback."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.workflow_schemas import DeliverableKind, StrictModel, local_text

CoordinationMode = Literal["off", "draft_only", "auto"]
Autonomy = Literal["draft", "ready", "auto_start"]
SignalSource = Literal[
    "internal", "schedule", "email", "webhook", "channel", "agent_finding", "monitor", "document"
]
SignalState = Literal["new", "matched", "ignored", "blocked", "triage"]
IntentState = Literal["pending", "leased", "done", "blocked", "unknown"]

WORK_ORDER_FILTER_KEYS = ("profile_id", "profile_name", "deliverable_kind", "origin_rule_id")
APPROVAL_FILTER_KEYS = ("action_type", "profile_id", "profile_name")

# Signal types an operator rule may match in this slice, with the normalized
# fields it may filter on. Free text such as titles is never matchable.
RULE_SIGNAL_TYPES: dict[str, tuple[str, ...]] = {
    "work_order.done": WORK_ORDER_FILTER_KEYS,
    "work_order.needs_review": WORK_ORDER_FILTER_KEYS,
    "work_order.failed": WORK_ORDER_FILTER_KEYS,
    "work_order.unknown": WORK_ORDER_FILTER_KEYS,
    "work_order.blocked": WORK_ORDER_FILTER_KEYS,
    "approval.approved": APPROVAL_FILTER_KEYS,
    "approval.rejected": APPROVAL_FILTER_KEYS,
}

# Which source produces each rule-matchable signal type.
SIGNAL_SOURCES: dict[str, str] = {key: "internal" for key in RULE_SIGNAL_TYPES}
SIGNAL_SOURCES["schedule.fired"] = "schedule"
RULE_SIGNAL_TYPES["schedule.fired"] = ()
SIGNAL_SOURCES["agent.finding"] = "agent_finding"
RULE_SIGNAL_TYPES["agent.finding"] = ("topic", "profile_id", "profile_name")
SIGNAL_SOURCES["email.received"] = "email"
RULE_SIGNAL_TYPES["email.received"] = ("from_domain", "from_address", "account_id", "account_label")
SIGNAL_SOURCES["webhook.received"] = "webhook"
RULE_SIGNAL_TYPES["webhook.received"] = ("webhook_id", "webhook_name", "event_type")
for _document_type in ("document.created", "document.changed"):
    SIGNAL_SOURCES[_document_type] = "document"
    RULE_SIGNAL_TYPES[_document_type] = ("source_id", "source_label", "mime_type")
# Unmatched signals from these sources go to Chief of Staff triage instead of being ignored.
TRIAGE_SOURCES = frozenset({"agent_finding", "email", "webhook", "channel", "monitor"})

FilterValue = str | list[str]
TIME_OF_DAY = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class Schedule(StrictModel):
    kind: Literal["daily", "weekly", "interval"]
    at: str | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    every_hours: int | None = Field(default=None, ge=1, le=168)

    @model_validator(mode="after")
    def complete(self) -> "Schedule":
        if self.kind in {"daily", "weekly"}:
            if self.at is None or not TIME_OF_DAY.match(self.at) or self.every_hours is not None:
                raise ValueError("daily and weekly schedules need a time as HH:MM and no hour interval")
            if (self.kind == "weekly") != (self.weekday is not None):
                raise ValueError("only weekly schedules take a weekday")
        elif self.every_hours is None or self.at is not None or self.weekday is not None:
            raise ValueError("interval schedules need every_hours only")
        return self


class QuietHours(StrictModel):
    start: str
    end: str

    @model_validator(mode="after")
    def valid_window(self) -> "QuietHours":
        if not TIME_OF_DAY.fullmatch(self.start) or not TIME_OF_DAY.fullmatch(self.end):
            raise ValueError("quiet hours need start and end as HH:MM")
        if self.start == self.end:
            raise ValueError("quiet hours must have different start and end times")
        return self


class RuleContent(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    source: str | None = None
    signal_type: str
    schedule: Schedule | None = None
    filters: dict[str, FilterValue] = Field(default_factory=dict, max_length=4)
    target_profile_id: UUID
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=8000)
    deliverable_kind: DeliverableKind = "operator_review"
    acceptance_checks: list[str] = Field(default_factory=list, max_length=8)
    autonomy: Autonomy = "draft"
    max_per_day: int = Field(default=10, ge=1, le=1000)
    max_model_attempts: int = Field(default=10, ge=0, le=1000)
    quiet_hours: QuietHours | None = None
    max_chain_depth: int | None = Field(default=None, ge=1, le=30)

    @field_validator("name", "title", "instructions")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)

    @field_validator("acceptance_checks")
    @classmethod
    def safe_checks(cls, value: list[str]) -> list[str]:
        if any(len(item) > 300 for item in value):
            raise ValueError("acceptance check is too long")
        return [local_text(item) for item in value]

    @model_validator(mode="after")
    def known_type_and_filters(self) -> "RuleContent":
        allowed = RULE_SIGNAL_TYPES.get(self.signal_type)
        if allowed is None:
            raise ValueError("signal type is not available to rules")
        expected = SIGNAL_SOURCES[self.signal_type]
        if self.source not in {None, expected}:
            raise ValueError(f"{self.signal_type} comes from source '{expected}'")
        self.source = expected
        if (expected == "schedule") != (self.schedule is not None):
            raise ValueError("a schedule is required for schedule rules and not allowed otherwise")
        for key, value in self.filters.items():
            if key not in allowed:
                raise ValueError(f"filter '{key}' is not available for {self.signal_type}")
            values = [value] if isinstance(value, str) else value
            if not values or len(values) > 20 or any(not item or len(item) > 200 for item in values):
                raise ValueError("filter values must be 1-20 non-empty strings of at most 200 characters")
        return self


class RuleCreate(RuleContent):
    pass


class RuleRevise(RuleContent):
    expected_version: int = Field(ge=1)


class RuleEnabled(StrictModel):
    enabled: bool
    expected_version: int = Field(ge=1)


class PauseChange(StrictModel):
    expected_version: int = Field(ge=0)


class AgentRuleAccessChange(StrictModel):
    enabled: bool
    expected_version: int = Field(ge=0)


class ToolCeiling(StrictModel):
    tools: list[str] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=0)


class RuleAgentActionOut(StrictModel):
    turn_id: UUID | None
    run_id: UUID | None
    actor_id: str
    decision: Literal["create_rule", "revise_rule", "disable_rule"]
    rule_version: int
    source_kind: Literal["operator_ui", "discord", "wechat", "agent_run"]


class RuleOut(StrictModel):
    id: UUID
    name: str
    enabled: bool
    version: int
    current_version: int
    source: str
    signal_type: str
    schedule: Schedule | None
    filters: dict[str, FilterValue]
    target_profile_id: UUID
    title: str
    instructions: str
    deliverable_kind: DeliverableKind
    acceptance_checks: list[str]
    autonomy: Autonomy
    max_per_day: int
    max_model_attempts: int = 10
    quiet_hours: QuietHours | None = None
    max_chain_depth: int | None
    created_at: datetime
    updated_at: datetime
    last_agent_action: RuleAgentActionOut | None = None


class StatusOut(StrictModel):
    mode: CoordinationMode
    max_chain_depth: int
    max_orders_per_day: int
    max_auto_starts_per_day: int
    timezone: str
    paused: bool
    agent_rules_enabled: bool
    version: int
    signal_types: dict[str, list[str]]
    profile_tool_ceiling: list[str]
    registered_tools: list[str]
    registered_tool_labels: dict[str, str] = Field(default_factory=dict)


class IntentOut(StrictModel):
    id: UUID
    action_kind: str
    rule_id: UUID | None
    rule_name: str | None
    rule_version: int | None
    state: IntentState
    autonomy: Autonomy | None
    error_code: str | None
    decision: dict[str, object] | None
    work_order_id: UUID | None
    work_order_title: str | None


class SignalOut(StrictModel):
    id: UUID
    source: SignalSource
    signal_type: str
    fields: dict[str, object]
    origin_work_order_id: UUID | None
    origin_run_id: UUID | None
    chain_depth: int
    state: SignalState
    reason: str | None
    created_at: datetime
    processed_at: datetime | None
    intents: list[IntentOut]
