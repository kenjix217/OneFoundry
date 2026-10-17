from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from onefoundry.coordination_rule_limits import quiet_until
from onefoundry.coordination_schemas import QuietHours


@pytest.mark.parametrize("hours", [{"start": "25:00", "end": "08:00"}, {"start": "08:00", "end": "08:00"}])
def test_invalid_quiet_hours_are_rejected(hours: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        QuietHours.model_validate(hours)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (6, datetime(2026, 10, 4, 8, tzinfo=UTC)),
        (8, None),
        (21, None),
        (22, datetime(2026, 10, 5, 8, tzinfo=UTC)),
    ],
)
def test_overnight_quiet_hours_and_exact_boundaries(hour: int, expected: datetime | None) -> None:
    assert (
        quiet_until({"start": "22:00", "end": "08:00"}, datetime(2026, 10, 4, hour, tzinfo=UTC), "UTC")
        == expected
    )


def test_daylight_saving_boundaries_and_disabled_window() -> None:
    assert quiet_until(None, datetime.now(UTC), "UTC") is None
    assert quiet_until(
        {"start": "00:00", "end": "01:30"}, datetime(2026, 11, 1, 4, 30, tzinfo=UTC), "America/New_York"
    ) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert quiet_until(
        {"start": "00:00", "end": "01:30"}, datetime(2026, 11, 1, 5, 45, tzinfo=UTC), "America/New_York"
    ) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert quiet_until(
        {"start": "00:00", "end": "02:30"}, datetime(2026, 3, 8, 5, 30, tzinfo=UTC), "America/New_York"
    ) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
