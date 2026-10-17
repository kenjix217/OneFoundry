"""Private draft-only database identity for separately approved capture access."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from onefoundry.db_roles import RuntimeRoleSettings


class MemoryCaptureDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_CAPTURE_", env_file=None, extra="forbid")

    db_user: str
    db_password: str = Field(min_length=1, repr=False)

    @field_validator("db_user")
    @classmethod
    def role(cls, value: str) -> str:
        return RuntimeRoleSettings.validate_role_name(value)

    @field_validator("db_password")
    @classmethod
    def password(cls, value: str) -> str:
        return RuntimeRoleSettings.reject_blank_password(value)
