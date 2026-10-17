# Isolated extension supervisor (opt-in)

The app container runs as UID 10001 and writes private `0700` directories and `0600` jobs under the Compose `runner_spool` volume. A shell run by the host operator's UID 1000 cannot read those jobs; the supervisor deliberately rejects a different owner. Use the separate `compose.extensions.yaml` service so it sees that volume as UID 10001. The app has no Docker socket. This trusted supervisor has the spool and Docker socket, but no database, model-secret volume, or application credentials. It launches each untrusted child without mounts, with network disabled, a read-only root, and a pinned image ID.

The optional service is source-only until the operator supplies a private config and starts it. Its Docker socket is effectively host-level authority, so only trusted supervisor code belongs in this service. The official [Docker CLI image](https://hub.docker.com/_/docker) supplies only the client executable; the supervisor does not run a nested daemon. Pin `EXTENSION_SUPERVISOR_DOCKER_CLI_IMAGE` to an exact registry digest, for example an approved `docker.io/library/docker:<version>-cli@sha256:<digest>` reference, and record the reviewed digest alongside the worker image ID. The build argument has no floating default.

Create a JSON config file outside the repository and outside every app mount, owned by UID 10001, mode `0600`, with an accessible parent directory. It must contain exactly these keys:

```json
{
  "spool_root": "/runner-spool/extensions",
  "image_id": "sha256:<exact-built-worker-image-id>",
  "docker_binary": "/usr/local/bin/docker",
  "docker_socket": "/var/run/docker.sock",
  "memory_mb": 256,
  "processes": 32,
  "cpus": 1,
  "max_timeout_seconds": 30,
  "max_output_bytes": 65536,
  "poll_seconds": 0.25
}
```

Use the same pinned worker image ID and resource values in app settings. Set `EXTENSION_SUPERVISOR_CONFIG` to the absolute host path of that config, `EXTENSION_SUPERVISOR_DOCKER_GID` to the numeric group ID of `/var/run/docker.sock`, and `EXTENSION_SUPERVISOR_DOCKER_CLI_IMAGE` to the reviewed digest reference. The bind-mounted config remains inaccessible to the app. The socket group is explicit because its numeric ID varies by host. At a later authorized activation, prepare the config outside the checkout, make its parent traversable by UID 10001, and set the file owner/mode to `10001:10001`/`0600`. Then use:

```sh
export EXTENSION_SUPERVISOR_CONFIG=/absolute/private/path/supervisor.json
export EXTENSION_SUPERVISOR_DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
export EXTENSION_SUPERVISOR_DOCKER_CLI_IMAGE='docker.io/library/docker:<reviewed-version>-cli@sha256:<reviewed-digest>'
docker compose -f compose.yaml -f compose.extensions.yaml up -d --build extension-supervisor
```

The digest example is a placeholder; replace it with a verified full SHA-256 reference before the build. The config must be a real JSON file already readable by UID 10001. Do not add the socket to the app or worker child.

`python -m runner.extension_worker_supervisor --config /run/extension-supervisor/config.json --once` processes one private job during offline acceptance without publishing a live-service heartbeat. A claim interrupted after launch becomes an `unknown` receipt and is never replayed automatically. A stopped supervisor leaves queued jobs pending; a queued job is not proof of readiness or execution. Keep the service running only when extension execution is intentionally enabled, and inspect exact receipts before reconciling unknown outcomes.

Queued jobs may include a hash-bound UTC `expires_at`. Under its exclusive attempt lock, the supervisor records `failed: worker_job_expired` for an unclaimed expired job before creating a claim or launching Docker. A prior claim still produces an `unknown` reconciliation receipt because its effect cannot be ruled out. The host checks expiry again immediately before a direct launch. The Gateway owns the queue deadline and cancellation protocol; it must not treat a stale queued job as completed work.

Each supervisor loop atomically replaces `extensions/heartbeat.json` (mode `0600`) with its pinned image ID, resource bounds, UTC time, and supervisor instance ID. The Gateway checks that private record against its configured freshness window and policy before admitting a new worker job. A stale heartbeat means the worker is unavailable for new work; an existing claim or unknown receipt still needs reconciliation. During a long single job, the heartbeat may become stale until that job finishes.

`make check-extension-isolation` builds a disposable worker image from the locked package context, runs all three real Docker isolation fixtures plus a joined Gateway/supervisor/receipt test on disposable PostgreSQL, and removes its unique test tag. It fails on skipped fixtures or a missing Docker daemon. The full `make check` runs this gate immediately before its PostgreSQL integration gate.
