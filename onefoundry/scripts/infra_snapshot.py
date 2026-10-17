"""Host-only Compose observer; run as the operator, never inside OneFoundry."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_compose_output(output: str, expected: list[str]) -> dict[str, Any]:
    if len(output) > 262_144:
        raise ValueError("Compose status output is too large")
    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    if len(rows) == 1 and isinstance(rows[0], list):
        rows = rows[0]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("Compose status output must contain objects")
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("Service")
        if name in expected:
            if name in by_name:
                raise ValueError("duplicate Compose service")
            by_name[name] = row
    services: list[dict[str, str]] = []
    for name in expected:
        row = by_name.get(name)
        if row is None:
            services.append({"name": name, "state": "missing", "health": "unknown"})
            continue
        raw_state = row.get("State")
        state = (
            "running"
            if raw_state == "running"
            else "stopped"
            if raw_state in {"exited", "dead", "created", "paused", "restarting"}
            else "unknown"
        )
        raw_health = row.get("Health")
        health = (
            raw_health
            if raw_health in {"healthy", "unhealthy", "starting"}
            else ("none" if raw_health == "" else "unknown")
        )
        services.append({"name": name, "state": state, "health": health})
    return {"captured_at": datetime.now(UTC).isoformat(), "services": services}


def capture(compose_file: Path, expected: list[str], output_path: Path) -> None:
    if not compose_file.is_file():
        raise ValueError("Compose file does not exist")
    if not expected or len(expected) > 16 or len(set(expected)) != len(expected):
        raise ValueError("expected services must be unique and nonempty")
    if any(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) is None for name in expected):
        raise ValueError("invalid service name")
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("Docker CLI is unavailable")
    result = subprocess.run(  # noqa: S603 - fixed read-only Compose argv; no shell
        [docker, "compose", "-f", str(compose_file), "ps", "--all", "--format", "json"],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    payload = parse_compose_output(result.stdout, expected)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_path.parent, prefix=".infra-", delete=False
        ) as file:
            temporary = file.name
            json.dump(payload, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o644)
        os.replace(temporary, output_path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture own Compose service status for OneFoundry")
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", action="append", required=True)
    parser.add_argument("--watch", action="store_true", help="repeat observations until interrupted")
    parser.add_argument("--interval-seconds", type=int, default=5)
    args = parser.parse_args()
    if args.interval_seconds < 2 or args.interval_seconds > 300:
        parser.error("interval-seconds must be between 2 and 300")
    try:
        while True:
            capture(args.compose_file.resolve(), args.service, args.output.resolve())
            if not args.watch:
                break
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Infra snapshot failed: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
