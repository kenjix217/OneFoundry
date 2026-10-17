"""Private, exact input-file storage contracts."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.input_file_bytes import InputFileBytes, InputFileInvalid


def test_store_and_verify_exact_file(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    store = InputFileBytes(root)
    project_id, file_id = uuid4(), uuid4()
    saved = store.write(project_id, file_id, [b"hello", b" world"], max_bytes=32)
    assert saved.byte_size == 11
    assert saved.sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert store.read(project_id, file_id, saved.sha256, saved.byte_size) == b"hello world"
    with pytest.raises(InputFileInvalid, match="does not match"):
        store.read(uuid4(), file_id, saved.sha256, saved.byte_size)
    with pytest.raises(InputFileInvalid, match="does not match"):
        store.read(project_id, file_id, "0" * 64, saved.byte_size)


def test_store_rejects_oversize_without_installing(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    store = InputFileBytes(root)
    project_id, file_id = uuid4(), uuid4()
    with pytest.raises(InputFileInvalid, match="limit"):
        store.write(project_id, file_id, [b"1234", b"5678"], max_bytes=7)
    assert list(root.rglob("*")) == []


def test_store_rejects_public_root_and_symlink(tmp_path: Path) -> None:
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(InputFileInvalid, match="private"):
        InputFileBytes(public)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    with pytest.raises(InputFileInvalid, match="symlink"):
        InputFileBytes(linked)
