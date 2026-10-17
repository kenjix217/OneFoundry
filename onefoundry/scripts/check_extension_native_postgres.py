"""Run extension cases on a private disposable native PostgreSQL cluster.

No installed database, application service, or Docker volume is selected.
Use --keep-for-browser only when a later local acceptance check needs this
exact test cluster; the printed path identifies a private env file and data.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg

PG_BIN = Path("/usr/lib/postgresql/18/bin")
REPO = Path(__file__).resolve().parents[1]
RUNNER = Path(sys.executable)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(*args: str, environment: dict[str, str] | None = None) -> None:
    subprocess.run(  # noqa: S603
        args, cwd=REPO, env=environment, check=True, timeout=120, capture_output=True, text=True
    )


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)


def _environment(root: Path, port: int) -> dict[str, str]:
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
        DB_PORT=str(port),
        DB_NAME="onefoundry",
        DB_USER="onefoundry",
        DB_PASSWORD=secrets.token_urlsafe(32),
        APP_DB_USER="onefoundry_app",
        APP_DB_PASSWORD=secrets.token_urlsafe(32),
    )
    _write_private(
        root / "test-db.env",
        "".join(
            f"export {key}={environment[key]}\n"
            for key in (
                "DB_HOST",
                "DB_PORT",
                "DB_NAME",
                "DB_USER",
                "DB_PASSWORD",
                "APP_DB_USER",
                "APP_DB_PASSWORD",
            )
        ),
    )
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-for-browser", action="store_true")
    parser.add_argument("tests", nargs="*", help="pytest paths or selectors")
    options = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="onefoundry-native-extension-", dir="/tmp"))
    root.chmod(0o700)
    data = root / "data"
    socket_dir = root / "sockets"
    socket_dir.mkdir(mode=0o700)
    port = _free_port()
    environment = _environment(root, port)
    password_file = root / "bootstrap-password"
    _write_private(password_file, environment["DB_PASSWORD"] + "\n")
    started = False
    try:
        _run(
            str(PG_BIN / "initdb"),
            "-D",
            str(data),
            "--username=onefoundry",
            f"--pwfile={password_file}",
            "--auth-local=trust",
            "--auth-host=scram-sha-256",
        )
        password_file.unlink()
        _run(
            str(PG_BIN / "pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -k {socket_dir}",
            "-w",
            "start",
        )
        started = True
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            dbname="postgres",
            user="onefoundry",
            password=environment["DB_PASSWORD"],
            autocommit=True,
        ) as connection:
            connection.execute("CREATE DATABASE onefoundry")
        for command in (
            (str(RUNNER), "-m", "alembic", "upgrade", "head"),
            (str(RUNNER), "-m", "onefoundry.db_roles"),
            (
                str(RUNNER),
                "-m",
                "pytest",
                "-q",
                *(options.tests or ("tests/test_gateway_extension_lifecycle_postgres.py",)),
            ),
        ):
            result = subprocess.run(command, cwd=REPO, env=environment, check=False)  # noqa: S603
            if result.returncode:
                return result.returncode
        return 0
    finally:
        password_file.unlink(missing_ok=True)
        if options.keep_for_browser and started:
            print(f"Private test DB retained: {root}")
            print(f"Environment file (0600): {root / 'test-db.env'}")
            print(f"Cleanup: {PG_BIN / 'pg_ctl'} -D {data} -m immediate -w stop")
            print(f"Then remove private directory: {root}")
        else:
            if started:
                try:
                    _run(str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    print(f"Could not stop private test DB; inspect {root}", file=sys.stderr)
                    raise
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
