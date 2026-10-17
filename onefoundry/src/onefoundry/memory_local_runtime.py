"""Resolve the installed private DB endpoint for the loopback memory companion."""

import ipaddress
import json
import logging
import subprocess

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from onefoundry.memory_plugin_service import run_memory_service
from onefoundry.memory_plugin_settings import MemoryPluginSettings
from onefoundry.settings import Settings


class MemoryLocalRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_LOCAL_", env_file=None, extra="forbid")

    database_container: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    database_network: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


def database_host(configured: MemoryLocalRuntimeSettings, *, deadline_seconds: int) -> str:
    # Host setup only: the plugin and its tools have no Docker command or credentials.
    result = subprocess.run(  # noqa: S603 - exact read-only arguments, validated identifiers
        [  # noqa: S607 - standard Docker CLI, outside the plugin transport
            "docker",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            configured.database_container,
        ],
        capture_output=True,
        timeout=deadline_seconds,
        check=True,
    )
    if len(result.stdout) > 65536:
        raise ValueError("database network inspection exceeds bounds")
    networks = json.loads(result.stdout)
    address = ipaddress.IPv4Address(networks[configured.database_network]["IPAddress"])
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise ValueError("database must use the installed private container network")
    return str(address)


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        configured = MemoryLocalRuntimeSettings()  # type: ignore[call-arg]
        policy = MemoryPluginSettings()  # type: ignore[call-arg]
        if policy.auth_mode != "api_key":
            raise ValueError("local companion requires its explicit read-only API-key policy")
        settings = Settings(db_host=database_host(configured, deadline_seconds=policy.deadline_seconds))
    except Exception:
        raise SystemExit(
            "memory_local_startup_failed: check private runtime configuration and installed DB"
        ) from None
    run_memory_service(policy, settings)


if __name__ == "__main__":
    main()
