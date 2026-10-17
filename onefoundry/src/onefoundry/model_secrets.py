"""Service-owned local API key files; database stores only account identifiers."""

import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

MAX_CREDENTIAL_BYTES = 8192


class SecretStoreError(Exception):
    pass


class ModelSecretStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _checked_root(self) -> None:
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            raise SecretStoreError("model credential directory unavailable") from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
        ):
            raise SecretStoreError("model credential directory permissions invalid")

    def _path(self, account_id: UUID) -> Path:
        return self.root / str(account_id)

    def check_ready(self) -> None:
        self._checked_root()

    def present(self, account_id: UUID) -> bool:
        self._checked_root()
        try:
            self._read_file(account_id)
        except FileNotFoundError:
            return False
        except SecretStoreError:
            return False
        return True

    def _read_file(self, account_id: UUID) -> str:
        info = self._path(account_id).lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
        ):
            raise SecretStoreError("model credential file permissions invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path(account_id), flags)
        except OSError:
            raise SecretStoreError("model credential unavailable") from None
        try:
            data = os.read(fd, MAX_CREDENTIAL_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_CREDENTIAL_BYTES or not data or b"\n" in data or b"\r" in data:
            raise SecretStoreError("model credential file invalid")
        try:
            value = data.decode("ascii")
        except UnicodeDecodeError:
            raise SecretStoreError("model credential file invalid") from None
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise SecretStoreError("model credential file invalid")
        return value

    def read(self, account_id: UUID) -> str:
        self._checked_root()
        try:
            return self._read_file(account_id)
        except FileNotFoundError:
            raise SecretStoreError("model credential unavailable") from None

    def write(self, account_id: UUID, api_key: str) -> None:
        self._checked_root()
        if (
            not api_key
            or len(api_key) > MAX_CREDENTIAL_BYTES
            or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
        ):
            raise SecretStoreError("invalid model API credential")
        temporary = self.root / f".{uuid4()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(api_key.encode("utf-8"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._path(account_id))
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, account_id: UUID) -> None:
        self._checked_root()
        self._path(account_id).unlink(missing_ok=True)

    def remove_orphans(self, active_ids: set[UUID]) -> None:
        self._checked_root()
        try:
            for path in self.root.iterdir():
                try:
                    account_id = UUID(path.name)
                except ValueError:
                    continue
                if str(account_id) == path.name and account_id not in active_ids:
                    path.unlink()
        except OSError:
            raise SecretStoreError("model credential cleanup failed") from None
