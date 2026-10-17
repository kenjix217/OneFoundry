"""Extension credentials survive unrelated model-account secret reconciliation."""

from pathlib import Path
from uuid import uuid4

from onefoundry.gateway_extension_credentials import credential_store
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings


def test_extension_namespace_is_independent_of_model_account_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(db_password="test-only", model_secret_root=str(root))
    identifier = uuid4()
    value = "synthetic-" + uuid4().hex
    store = credential_store(settings)
    store.write(identifier, value)
    ModelSecretStore(str(root)).remove_orphans(set())
    assert store.read(identifier) == value
    assert store.root == root / "extension-connections"
    assert store.root.stat().st_mode & 0o777 == 0o700
