"""Real Docker checkpoint covers private host configuration and coding evidence."""

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from scripts import onefoundry_update as installer
from tests.test_maintenance_transport_config import configured


@pytest.mark.integration
def test_private_bindings_restore_with_volume_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "onefoundry_checkpoint_" + uuid4().hex[:12]
    volume = project + "_fixture"
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)  # noqa: S603,S607 - unique test volume
    try:
        path, _value, spool = configured(tmp_path)
        original_config = path.read_bytes()
        receipt = spool / "receipts/proof.json"
        receipt.write_text('{"status":"known"}')
        receipt.chmod(0o640)
        monkeypatch.setattr(installer, "volume_names", lambda *_: [volume])
        monkeypatch.setattr(installer, "validate_manifest", lambda _: {"version": "1.0.0"})
        backup = installer.snapshot(tmp_path, tmp_path, project)
        manifest = json.loads((backup / "manifest.json").read_text())
        assert set(manifest["bindings"]) == {"private-config", "maintenance-transport"}
        assert all((backup / f"{name}.tar").stat().st_mode & 0o777 == 0o600 for name in manifest["bindings"])
        (path.parent / "temporary-setting.json").write_text("new setting")
        receipt.write_text('{"status":"changed"}')
        with pytest.raises(ValueError, match="installation home"):
            installer.restore(backup, project, "1.0.0")
        installer.restore(backup, project, "1.0.0", home=tmp_path)
        assert path.read_bytes() == original_config
        assert not (path.parent / "temporary-setting.json").exists()
        assert json.loads(receipt.read_text()) == {"status": "known"}
        assert receipt.stat().st_uid == os.getuid()
        assert installer.maintenance_transport_override(tmp_path)
        (backup / "maintenance-transport.tar").write_bytes(b"changed")
        with pytest.raises(ValueError, match="identity changed"):
            installer.restore(backup, project, "1.0.0", home=tmp_path)
    finally:
        subprocess.run(["docker", "volume", "rm", volume], check=True, capture_output=True)  # noqa: S603,S607 - only this test's volume
