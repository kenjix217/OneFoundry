"""Schedule due-time rules: no backfill before creation, missed periods collapse."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from onefoundry.coordination_schemas import RuleCreate, Schedule
from onefoundry.coordination_store import latest_due

NY = ZoneInfo("America/New_York")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_daily_fires_latest_slot_after_creation_only() -> None:
    schedule = {"kind": "daily", "at": "09:00"}
    anchor = at("2026-10-01T10:00:00")
    assert latest_due(schedule, anchor, at("2026-10-01T23:00:00"), UTC) is None
    assert latest_due(schedule, anchor, at("2026-10-02T09:00:00"), UTC) == at("2026-10-02T09:00:00")
    # Three missed days collapse to the most recent slot.
    assert latest_due(schedule, anchor, at("2026-10-05T12:00:00"), UTC) == at("2026-10-05T09:00:00")


def test_daily_uses_the_configured_time_zone() -> None:
    due = latest_due(
        {"kind": "daily", "at": "09:00"}, at("2026-10-01T00:00:00"), at("2026-10-02T14:00:00"), NY
    )
    assert due == datetime(2026, 10, 2, 9, 0, tzinfo=NY)


def test_weekly_and_interval() -> None:
    weekly = {"kind": "weekly", "at": "08:30", "weekday": 0}  # Monday
    anchor = at("2026-10-01T00:00:00")  # Thursday
    assert latest_due(weekly, anchor, at("2026-10-04T12:00:00"), UTC) is None
    assert latest_due(weekly, anchor, at("2026-10-07T12:00:00"), UTC) == at("2026-10-05T08:30:00")
    interval = {"kind": "interval", "every_hours": 6}
    assert latest_due(interval, anchor, at("2026-10-01T05:59:00"), UTC) is None
    assert latest_due(interval, anchor, at("2026-10-01T13:00:00"), UTC) == at("2026-10-01T12:00:00")


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "daily"},
        {"kind": "daily", "at": "25:00"},
        {"kind": "daily", "at": "09:00", "weekday": 1},
        {"kind": "weekly", "at": "09:00"},
        {"kind": "interval", "every_hours": 0},
        {"kind": "interval", "every_hours": 2, "at": "09:00"},
    ],
)
def test_invalid_schedules_are_rejected(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Schedule.model_validate(value)


def test_schedule_rules_need_a_schedule_and_derive_their_source() -> None:
    base = {
        "name": "Weekly brief",
        "target_profile_id": "11111111-1111-1111-1111-111111111111",
        "title": "Weekly research brief",
        "instructions": "Summarize the week.",
    }
    rule = RuleCreate.model_validate(
        {**base, "signal_type": "schedule.fired", "schedule": {"kind": "weekly", "at": "08:00", "weekday": 0}}
    )
    assert rule.source == "schedule"
    with pytest.raises(ValidationError):
        RuleCreate.model_validate({**base, "signal_type": "schedule.fired"})
    with pytest.raises(ValidationError):
        RuleCreate.model_validate(
            {**base, "signal_type": "work_order.done", "schedule": {"kind": "interval", "every_hours": 1}}
        )
    with pytest.raises(ValidationError):
        RuleCreate.model_validate({**base, "signal_type": "work_order.done", "source": "email"})
