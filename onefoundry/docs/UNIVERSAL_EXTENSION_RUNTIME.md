# Universal extensions runtime

Integrations and Tool Foundry are views of the existing Tool Gateway. Packages
start inactive. Accepting an exact package version admits its definitions; role
grants, project connection bindings, credentials, worker readiness, and each
remote invocation's approval remain separate requirements. A `SKILL.md` supplies
instructions and never grants a tool or a destination.

## Configuration

The existing typed `Settings` owner applies explicit constructor values before
environment values, then defaults. It does not load an env file. Docker Compose
reads `.env` and passes settings explicitly. Invalid values fail validation.
See [the complete safe sample](universal-extension.env.example).

| Environment variable | Default | Accepted values / purpose |
| --- | --- | --- |
| `EXTENSION_ALLOWED_ORIGINS` | `[]` | JSON list, at most 64 distinct exact HTTP(S) origins; no paths, credentials, query or fragments. Empty denies generic network access. |
| `EXTENSION_TIMEOUT_SECONDS` | `10` | Integer 1–120; bounded remote call and worker request deadline. |
| `EXTENSION_MAX_INPUT_BYTES` | `65536` | Integer 1024–1048576; serialized tool input limit. |
| `EXTENSION_MAX_RESPONSE_BYTES` | `262144` | Integer 1024–4194304; remote response limit. |
| `EXTENSION_WORKER_IMAGE` | empty | Empty disables worker admission; otherwise exact lowercase `sha256:` image ID with 64 hex digits. |
| `EXTENSION_WORKER_MEMORY_MB` | `256` | Integer 128–1024; must match supervisor policy. |
| `EXTENSION_WORKER_PROCESSES` | `32` | Integer 8–64; must match supervisor policy. |
| `EXTENSION_WORKER_CPUS` | `1` | Integer 1–2; must match supervisor policy. |
| `EXTENSION_WORKER_MAX_OUTPUT_BYTES` | `65536` | Integer 1024–65536; bounded worker output. |
| `EXTENSION_WORKER_POLL_SECONDS` | `0.25` | Number 0.1–10; Gateway reconciliation interval. |
| `EXTENSION_WORKER_HEARTBEAT_SECONDS` | `30` | Integer 5–300; maximum supervisor heartbeat age for new admission. |
| `EXTENSION_WORKER_QUEUE_SECONDS` | `60` | Integer 5–600; queue/resume publication deadline. |
| `EXTENSION_WEBHOOK_MAX_BYTES` | `65536` | Integer 1024–1048576; signed raw intake body limit. |
| `EXTENSION_WEBHOOK_WINDOW_SECONDS` | `300` | Integer 30–900; signed delivery timestamp window. |

Allowed origins are an additional network restriction, not consent to send
company data. Redirects, proxies, private/link-local destinations and DNS changes
cannot expand that list. Explicit loopback fixture origins are supported.
Connection tests are operator requested, bounded diagnostics; they never grant
discovered tools. MCP calls check their declared input schema against the live
server before execution. Server annotations do not grant read or write authority.

Credentials are entered through the protected connection credential endpoint,
stored in the existing private credential root's `extension-connections`
namespace, and resolved only by the transport owner. The database stores opaque
references. Do not put keys in manifests, instruction text, source, URLs or
examples. A connection version changes when its credentials or configuration
change.

Skills reference canonical tool IDs. Configure inside a skill opens the shared
project connection editor; tool package slots own the saved binding. Binding
changes invalidate prior run snapshots, including changing A to B and back.
Runs also pin package/descriptor identity, connection version and worker image.
Installed tool IDs encode the full SHA-256 of the package key and declared tool
name, within the provider's 64-character limit. The catalog shows readable local
names. Reviewed identities remain reserved after revocation; only the same
package key and local tool name may retain a grant across a version upgrade.
Earlier unreleased development IDs are not aliases: if reusing a pre-release
database, review and grant the new IDs explicitly. Released built-in IDs are
unchanged. Package activation decisions serialize within the project.
Every generic HTTP/MCP invocation, including reads, requires an approval for its
exact destination, operation and disclosed input. Distinct call IDs represent
distinct intent; retrying a known call ID reads its receipt. Unknown effects must
be reconciled before any intentional new attempt. Execution start is recorded
separately from proposal creation. A lost remote attempt becomes unknown after
the request deadline plus the queue reconciliation window, even with no Python
worker configured; it is never automatically resent.

## SDK and isolated execution

The independently packaged SDK lives in `sdk/python`. From the locked app
environment, use `uv run onefoundry-sdk init DIRECTORY --id example --name Example`,
then `validate`, `test`, `build --output ARCHIVE`, and `inspect ARCHIVE`.
`test` validates JSON fixtures and schemas without executing package code and
reports `execution: not_run`. Real pure-function fixtures run with
`python -m runner.extension_fixture_test --help` through the pinned isolated
worker. Connected behavior needs Gateway admission and exact approvals.

Use the [account-brief reference package](../sdk/python/examples/account-brief/README.md)
for a complete synthetic example. Its joined acceptance test uses one exact
archive for API/MCP lookup, the pure Python formatter and separately approved
outbound delivery, plus an inbound subscription that creates one draft work order.

Archives are inert: bounded ZIP members, validated paths, no symlinks, no install
hooks, no arbitrary dependency installation. Dependencies belong in a reviewed,
locked worker image. Static resources and LICENSE/NOTICE files are preserved.
Package code runs in a disposable child with no network, no host mounts, a
read-only root and bounded resources. The app never imports package code or gets
a Docker socket. Declared connection callbacks return to the same Tool Gateway.

Use the [opt-in supervisor guide](EXTENSION_SUPERVISOR.md) for the separate
trusted host service. A configured image alone is insufficient: admission checks
a fresh private heartbeat and matching resource policy. An interrupted claim
produces an unknown result and cannot replay automatically. Expired unclaimed
jobs are cancelled without launch; claimed jobs with no conclusive receipt remain
unknown. Cancellation is recorded before reconciliation; it is not presented as
proof that an already launched remote effect was undone.

## Webhooks and portability

Inbound subscriptions start disabled. The write-only signing credential is
stored privately. Send `X-OneFoundry-Timestamp` (Unix seconds), a unique
`X-OneFoundry-Delivery`, and `X-OneFoundry-Signature: sha256=…`, where the digest is
HMAC-SHA256 of `timestamp + "." + delivery_id + "." + raw_body`. Verification
precedes JSON parsing. Repeated delivery ID and body returns the same durable
receipt; changed bytes under the same ID conflict. The trusted subscription owns
project and actor attribution. A receipt emits `webhook.received` to existing
automation; it does not mean a work order completed. `COORDINATION_MODE=draft_only`
keeps resulting work in drafts. This localhost receiver does not establish public
ingress. Outbound webhooks use ordinary approval-bound HTTP write tools.

Native import/export preserves exact package bytes. Codex portable, legacy
Codex and Claude formats have separate parsers and per-component reports. Static
skills, resources, provenance and inactive HTTPS MCP templates are supported.
Hooks, shell commands, host agents/UI and foreign permissions remain inactive or
are rejected. An exported package still needs the target host's configuration;
there is no cross-host permission transfer or arbitrary OneFoundry control MCP.

## Verification

`make check` includes SDK tests/type checks/build, extension unit checks, the
real Docker isolation and joined Gateway end-to-end gate (three child fixtures
and two Gateway cases), the repository PostgreSQL regression
suite, and frontend checks. `scripts/check_extension_native_postgres.py` offers
a disposable native PostgreSQL acceptance environment; it does not migrate the
installed database. The existing maintenance assertion fixture requires
`ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE`: build the Dockerfile `repository-qa` target,
resolve its immutable image ID and export that value before the full gate. Record skipped/unavailable checks as incomplete. Current
implementation evidence and remaining gates are tracked in
[the execution plan](UNIVERSAL_EXTENSION_IMPLEMENTATION.md).

The existing offline browser fixture also needs an image to run rather than
skip: build `runner/browser.Dockerfile` with a disposable tag, resolve the local
immutable image ID and pass it as `ONEFOUNDRY_BROWSER_TEST_IMAGE`. This synthetic
fixture is separate from real product desktop/mobile/keyboard/zoom acceptance.
