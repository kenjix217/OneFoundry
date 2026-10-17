"""Run extension integration cases on one disposable local PostgreSQL instance.

The installed application and its volumes are never selected. Test credentials
are generated in memory and a private temporary env file, never printed.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

import psycopg


def main() -> int:
    name = f"onefoundry-extension-test-{uuid4().hex[:12]}"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "HOME",
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "UV_CACHE_DIR",
            "ONEFOUNDRY_EXTENSION_TEST_IMAGE",
        }
    }
    environment.update(
        DB_HOST="127.0.0.1",
        DB_NAME="onefoundry",
        DB_USER="onefoundry",
        DB_PASSWORD=secrets.token_urlsafe(32),
        APP_DB_USER="onefoundry_app",
        APP_DB_PASSWORD=secrets.token_urlsafe(32),
    )
    with tempfile.TemporaryDirectory(prefix="onefoundry-extension-db-") as temporary:
        config = Path(temporary) / "db.env"
        config.write_text(
            "POSTGRES_DB=onefoundry\nPOSTGRES_USER=onefoundry\nPOSTGRES_PASSWORD="
            + environment["DB_PASSWORD"]
            + "\n"
        )
        config.chmod(0o600)
        try:
            subprocess.run(  # noqa: S603 - fixed Docker executable and generated test-only arguments
                [  # noqa: S607 - operator-selected PATH, test harness only
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--env-file",
                    str(config),
                    "--publish",
                    "127.0.0.1::5432",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,nosuid,nodev",
                    "postgres:16.10-alpine",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )  # noqa: S603,S607
            port = (
                subprocess.check_output(["docker", "port", name, "5432"], text=True, timeout=10)  # noqa: S603,S607
                .strip()
                .rsplit(":", 1)[1]
            )  # noqa: S603,S607
            environment["DB_PORT"] = port
            deadline = monotonic() + 45
            while True:
                try:
                    with psycopg.connect(
                        host="127.0.0.1",
                        port=int(port),
                        dbname="onefoundry",
                        user="onefoundry",
                        password=environment["DB_PASSWORD"],
                        connect_timeout=1,
                    ):
                        break
                except psycopg.OperationalError:
                    if monotonic() >= deadline:
                        raise RuntimeError("disposable PostgreSQL did not become ready") from None
                    Event().wait(0.1)
            for command in (
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                [sys.executable, "-m", "onefoundry.db_roles"],
                [sys.executable, "-m", "pytest", "-q", *sys.argv[1:]],
            ):
                result = subprocess.run(command, env=environment, check=False)  # noqa: S603
                if result.returncode:
                    return result.returncode
            return 0
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False, timeout=30)  # noqa: S603,S607


if __name__ == "__main__":
    raise SystemExit(main())
