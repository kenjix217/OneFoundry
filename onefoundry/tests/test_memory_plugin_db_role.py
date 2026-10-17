"""Reject unsafe provisioning inputs before contacting PostgreSQL."""

import secrets
from unittest.mock import patch

import pytest

from onefoundry.db_roles import RuntimeRoleSettings
from onefoundry.memory_plugin_db_role import provision_memory_read_role
from onefoundry.settings import Settings


@pytest.mark.parametrize("deadline", [0, 1, 61, True])
def test_invalid_deadline_never_contacts_database(deadline: int) -> None:
    with patch("onefoundry.memory_plugin_db_role.connect") as connect:
        with pytest.raises(ValueError, match="deadline"):
            provision_memory_read_role(
                Settings(db_password=secrets.token_urlsafe(32)),
                RuntimeRoleSettings(app_db_user="memory_fixture", app_db_password=secrets.token_urlsafe(32)),
                deadline_seconds=deadline,
            )
        connect.assert_not_called()


def test_bootstrap_identity_cannot_be_repurposed() -> None:
    with patch("onefoundry.memory_plugin_db_role.connect") as connect:
        with pytest.raises(ValueError, match="differ"):
            provision_memory_read_role(
                Settings(db_user="memory_fixture", db_password=secrets.token_urlsafe(32)),
                RuntimeRoleSettings(app_db_user="memory_fixture", app_db_password=secrets.token_urlsafe(32)),
                deadline_seconds=10,
            )
        connect.assert_not_called()
