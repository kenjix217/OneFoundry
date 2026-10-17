"""Input intake limits are validated at the configuration boundary."""

import pytest
from pydantic import ValidationError

from onefoundry.settings import Settings


def test_input_limits_and_root_are_validated() -> None:
    settings = Settings(db_password="test-only", input_file_max_bytes=1024)
    assert settings.input_file_max_bytes == 1024
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", input_file_max_bytes=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", input_file_root="../files")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", input_file_max_project_bytes=1024)
    with pytest.raises(ValidationError):
        Settings(db_password="test-only", input_file_max_visual_count=0)


def test_visual_limit_setting_uses_environment_with_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INPUT_FILE_MAX_VISUAL_COUNT", "3")
    assert Settings(db_password="test-only").input_file_max_visual_count == 3
    assert Settings(db_password="test-only", input_file_max_visual_count=1).input_file_max_visual_count == 1
    monkeypatch.setenv("INPUT_FILE_MAX_VISUAL_COUNT", "not-a-number")
    with pytest.raises(ValidationError):
        Settings(db_password="test-only")
