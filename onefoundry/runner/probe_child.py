"""Fixed offline edit executed only inside the local-source probe sandbox."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from uuid import UUID


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        attempt_id = str(UUID(sys.argv[1]))
    except ValueError:
        return 2
    network_namespace = os.readlink("/proc/self/ns/net")
    if attempt_id != sys.argv[1] or Path.cwd() != Path("/workspace") or network_namespace == sys.argv[2]:
        return 2
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.1)
        try:
            connection.connect(("192.0.2.1", 9))
        except OSError:
            network_connect = "blocked"
        else:
            network_connect = "connected"
    path = Path(f"onefoundry-probe-{attempt_id}.txt")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(f"OneFoundry offline source probe {attempt_id}\n".encode("ascii"))
    observation = {
        "network_namespace": network_namespace,
        "network_connect": network_connect,
        "root_entries": sorted(os.listdir("/")),
        "environment_keys": sorted(os.environ),
    }
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
