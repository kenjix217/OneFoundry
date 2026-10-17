"""Install the pinned Gitleaks release into a caller-owned local path."""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "8.30.1"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
ARCHIVE_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
URL = "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"


def install(destination: Path) -> None:
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Pinned Gitleaks binary requires Linux x86-64")
    with urllib.request.urlopen(URL, timeout=30) as response:
        archive = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("Gitleaks archive exceeds size limit")
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Gitleaks archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as contents:
        member = next(
            (item for item in contents if item.name == "gitleaks" and item.isfile()),
            None,
        )
        if member is None:
            raise ValueError("Gitleaks archive has no executable")
        source = contents.extractfile(member)
        if source is None:
            raise ValueError("Gitleaks executable could not be read")
        binary = source.read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(binary)
    temporary.chmod(0o700)
    temporary.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    install(args.destination)
    print(f"Installed checksum-verified Gitleaks v{VERSION}")


if __name__ == "__main__":
    main()
