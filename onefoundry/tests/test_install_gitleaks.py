"""The local scanner installer fails safely on bad or missing releases."""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path
from urllib.error import URLError

import pytest

from scripts import install_gitleaks


def _archive(member: str = "gitleaks") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        payload = b"synthetic executable for installer test"
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _mock_download(monkeypatch: pytest.MonkeyPatch, archive: bytes) -> None:
    monkeypatch.setattr(install_gitleaks, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(archive),
    )


def test_installer_writes_verified_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_download(monkeypatch, _archive())
    destination = tmp_path / "tools" / "gitleaks"
    install_gitleaks.install(destination)
    assert destination.read_bytes() == b"synthetic executable for installer test"
    assert destination.stat().st_mode & 0o111


def test_installer_rejects_checksum_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(_archive()),
    )
    destination = tmp_path / "gitleaks"
    destination.write_bytes(b"existing executable")
    with pytest.raises(ValueError, match="checksum"):
        install_gitleaks.install(destination)
    assert destination.read_bytes() == b"existing executable"


@pytest.mark.parametrize("member", ["other", "../gitleaks"])
def test_installer_rejects_missing_or_unsafe_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str
) -> None:
    _mock_download(monkeypatch, _archive(member))
    destination = tmp_path / "gitleaks"
    with pytest.raises(ValueError, match="no executable"):
        install_gitleaks.install(destination)
    assert not destination.exists()


def test_installer_rejects_oversized_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_gitleaks, "MAX_ARCHIVE_BYTES", 20)
    _mock_download(monkeypatch, b"x" * 21)
    with pytest.raises(ValueError, match="size limit"):
        install_gitleaks.install(tmp_path / "gitleaks")
    assert not (tmp_path / "gitleaks").exists()


def test_installer_rejects_unsupported_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(RuntimeError, match="Linux x86-64"):
        install_gitleaks.install(tmp_path / "gitleaks")
    assert not (tmp_path / "gitleaks").exists()


def test_installer_propagates_download_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> io.BytesIO:
        raise URLError("synthetic network failure")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    destination = tmp_path / "gitleaks"
    destination.write_bytes(b"existing executable")
    with pytest.raises(URLError):
        install_gitleaks.install(destination)
    assert destination.read_bytes() == b"existing executable"
