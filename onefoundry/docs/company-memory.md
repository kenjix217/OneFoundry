# Shared company memory

OneFoundry's existing Postgres `memory_items` remains the authoritative reviewed
fact store. Codex, cloud Dot, and internal agents use the same retrieval owner.
Captured transcripts live separately in `memory_captures`; they are source
archives, never automatically published facts. This service does not discover
private chats, hidden host memory, assistant instructions, or mail.

## Knowledge from coding sessions

The OneFoundry Company Memory plugin lets a connected Codex workspace retrieve
approved context before coding and, when explicitly requested and granted,
propose selected knowledge for retention. Useful material includes architecture
decisions, repository constraints, build/test steps, and lessons from a repair.
Internal role agents retrieve the same approved store. Project scope keeps facts
in their source project; company scope intentionally shares them across projects.

For example: “Propose saving this architecture decision and these build/test
steps to this project's OneFoundry memory.” The client previews the exact selected
excerpt, factual summary, source, and scope, then waits for confirmation before
submission. The local operator reviews the pending candidate before it enters
approved retrieval. Capture access is optional and off by default; read access
does not grant it. Capture does not automatically collect all coding activity or
synchronize entire codebases.
The developer's plugin connection is separate from the isolated coding worker;
connecting it does not grant that worker memory access.

## Operator flow

Select a project → Memory → Capture conversation. Enter a source reference,
original message time, visible speaker, excerpt, and curated fact. Confirm that
the company content is authorized. Select an existing fact to propose a
correction. Go to Browse & review → Inspect source → approve/reject. Until review,
the existing fact remains current. Approval publishes the replacement atomically
and marks the previous fact superseded, retaining its source and replacement ID.

Browse & review → Withdraw fact → enter reason → Confirm withdraw excludes the
fact from every current retrieval path. History → Restore fact reverses a
withdrawal. Superseded facts cannot be restored over their approved replacement.
Source archives and historical facts have no delete endpoint. Permanent erasure
requires a separately confirmed, reviewed procedure; schema downgrade refuses
to discard source or context evidence.

Capture is bounded to 16 visible user/assistant messages, 4096 UTF-8 bytes per
message/fact, and 16384 message-text bytes per request. Timezone-aware source
timestamps are mandatory. Secret-shaped content, control/tool markers, hidden
speaker roles and unknown fields are rejected before storage. Each capture has
project, actor, source reference, original timestamps, capture time, SHA-256 and
caller idempotency ID. Reusing an ID with changed content fails. No model can
approve a capture through MCP. Operator events contain references and reasons,
not transcripts or queries.

## Local API and MCP

The existing localhost operator API adds:

| Route under `/api/projects/{project_id}` | Purpose |
| --- | --- |
| `POST /memory/captures` | Explicit source archive + pending DreamCycle proposal |
| `GET /memory/captures/{source_id}` | Operator source inspection in its owning project |
| `GET /memory` | Bounded configured current-fact retrieval |
| `GET /memory/{id}` | Current fact; operator-only `include_history=true` inspects history |
| `GET /memory/history` | Bounded owning-project withdrawn/superseded history |
| `POST /memory/{id}/state` | Operator `withdraw`/`restore` with reason |
| `POST /candidates/{id}/review` | Existing operator-only approved/rejected decision |

The separate authenticated HTTP MCP service exposes `memory_search`,
`memory_read`, and optionally selected capture tools. There are no source-archive,
history, approval, deletion, SQL, profile, or arbitrary-control tools. Both
`project_id` and optional company scope are checked against server-owned grants
for the verified bearer identity's exact subject/client pair. Omitted scope is project-only
unless `company_read` is explicitly granted. Company scope is intentionally
shared across the single company's projects. Mutating a company fact still
requires its owning project and scope. External actors are distinct from the
operator. Search/read return bounded current facts and source IDs for citation.

Internal live text, conversation, role reads, and chief context already use the
same store. Team turns now select scoped approved facts and persist exact fact
text/references with their existing input digest. Repeated preparation of the
same attempt reuses its historical snapshot; new attempts retrieve current facts.
The existing `ROLE_CONTEXT_MAX_ITEMS` setting (1–20) and context byte budget bound
new team fact selection; no separate team-memory limit or configuration hierarchy
is introduced.

## Private companion deployment

`onefoundry.memory_plugin_db_role` provisions a **new** dedicated login in one
transaction, using the existing typed `DB_*` owner settings and `APP_DB_*` target
settings. It refuses existing identities and missing lifecycle columns. Grants
are `CONNECT`, schema `USAGE`, `SELECT` on projects/facts and (schema 0072+) derived vectors, and `INSERT` on events
for Tool Gateway access receipts. There are no transcript, profile, approval,
fact-write, event-read, or sequence grants. This role supports read access only;
keep capture grants false. Credential creation and live grants need approval.

Use [the Compose sample](company-memory-compose.env.example) and run
`python -m onefoundry.memory_companion_settings` before the separately approved
`docker compose -p <dedicated-memory-project> -f compose.memory.yaml up -d`.
Deployment settings are explicit typed values over `MEMORY_*` environment values;
all five values are required, and no dotenv file loads automatically. Image IDs
or registry digests must be SHA-256 pinned, paths absolute, and the private env
an owned regular file with owner-only permissions. The public key must have no
other writers and be owned by container UID 10001 for runtime validation.

The companion has no published port, runs non-root with a read-only filesystem,
drops capabilities, and mounts only the public verification key. It joins the
approved existing private DB network and its separate outbound network. An
approved tunnel-client must share its network namespace to reach the loopback
MCP listener; a host port mapping cannot reach container loopback. Do not add
services to the installed app's Compose project or expose PostgreSQL.

The private catalog at `.agents/plugins/marketplace.json` references the existing
plugin. Approved registration uses `codex plugin marketplace add <onefoundry-root>`
and `codex plugin add onefoundry-company-memory@onefoundry-local`. A source-only
installation supplies the skill; it still needs the verified MCP/Tunnel binding
and supported authentication before company-memory tools are usable.

### Local Codex API key

Auth0 is optional. Set `MEMORY_PLUGIN_AUTH_MODE=api_key` with
`MEMORY_PLUGIN_API_KEY_RECORDS_PATH` and read-only grants; leave OAuth settings
unset. [The local sample](company-memory-local.env.example) lists required values.
The owned mode-0600 records file contains only unique SHA-256 digests, exact
subject/client grant identities, and Unix expiry times. Emptying the list revokes
all keys immediately. Unknown actors, unsafe files, invalid records and expired
keys fail closed. API-key mode has no OAuth discovery. Existing records without
`scopes` retain only `memory.read` and expose exactly the two read tools.

`onefoundry.memory_local_runtime` resolves the selected installed DB container's
private IPv4 address at startup and launches the same loopback service. Its
`MEMORY_LOCAL_DATABASE_CONTAINER` and `MEMORY_LOCAL_DATABASE_NETWORK` settings
are required typed identifiers, with explicit values over environment. Only
bootstrap uses read-only Docker inspection; tools cannot access Docker or DB
credentials. Supervise this separate process as an operator-owned user service.
Restart it after app upgrades to resolve any changed database container address.

Package with `--local-client-config <absolute-private-client-JSON-path>` to bind
the stdio bridge. Client JSON contains `server_url`, `api_key`, `project_ids`,
`timeout_seconds` (2–60), `max_request_bytes` (4096–131072) and
`max_response_bytes` (4096–2097152). The file must be owned mode 0600; its contents
never enter the plugin archive. The bridge reloads it for each call, refuses
redirects/proxies and supplies the selected project IDs at native initialization.
The server still enforces its own grants. Local installation does not make this
stdio process available to cloud Dot; cloud reachability and its native supported
authentication need separate acceptance.

### Selective API-key capture (optional)

Capture is off by default. An additional explicitly approved identity/key may
have both `memory.read` and `memory.capture`; its server grant must also enable
`capture`. Company-wide publication requires `company_read` and `company_capture`
as well. Existing reader identities, keys and database permissions stay unchanged.
The bridge reloads private key configuration; the cloud host needs a refreshed
tool catalog after a separately approved switch to the capture identity.

`memory_capture_preview` returns the exact selected visible messages, source,
fact summary, project, scope and optional replacement ID. It stores only a
bounded expiring digest in process memory and content-free access receipts.
Present that selection and the source-retention notice to the human, then wait
for confirmation before submitting identical content to `memory_capture` with
the returned `preview_sha256`. The preview expires in 300 seconds by default
(30–900 via `MEMORY_PLUGIN_CAPTURE_PREVIEW_SECONDS`); a restart loses previews.
The server enforces matching selection, identity, scope, size and explicit
authorization fields. Human consent is a client procedure, not something a
digest can prove. Enabling access never authorizes ambient or whole-chat import.

Submission archives the selected excerpt and creates a **pending** DreamCycle
candidate through the existing Tool Gateway owner. Rejecting the candidate
retains its source archive. The candidate is excluded from approved retrieval
until local operator review; hybrid queries also require the existing bounded
indexer after approval. Retries reuse the unchanged `client_capture_id`; changed
content under that ID conflicts. Corrections retain historical provenance.

After separately approving schema `0073_memory_capture_lock`, provision a new
private draft login with `python -m onefoundry.memory_plugin_capture_db_role` in
the approved owner environment and the typed
[capture settings](company-memory-capture.env.example). The helper only acquires
the existing project row lock; its function has a pinned search path and no
PUBLIC execution grant. Draft credentials remain server-side. The login can
insert bounded source/result/pending rows and Gateway events and read lineage
hashes/receipts. It cannot read archived messages, update/delete rows, publish
facts, insert candidate status, review or erase history. Startup rejects widened
draft privileges. No new network listener, tunnel or raw database export is needed.

Rollout requires an approved backup, migration, dedicated draft login, separate
capture actor/project/key, companion update and private host tool refresh.
Revoke the additional key first to disable capture; return the bridge to its
existing read key and remove the capture grant/config before restarting both
companion and dependent tunnel services. This retains sources and pending facts.
No source deletion or downgrade is part of disabling capture.

## Configuration

Use [the sample](company-memory.env.example) only in the approved companion's
private launch environment. No `.env` file loads automatically. Explicit typed
settings override environment; safe defaults apply only to bounds, never to
identity, grants, database credentials, key, issuer, port or audience.

`public_url` uses HTTPS `/mcp`, or the exact HTTP loopback listener for an approved
Secure MCP Tunnel. In tunnel mode, `oauth_audience` pins the actual external HTTPS
resource separately from local discovery metadata. `oauth_issuer` must be HTTPS.
`oauth_public_key_path` must be an absolute, owned regular RSA public-key PEM file
without other writers. Only RS256 is accepted. Rotate the public key deliberately
and recheck connection before retiring an issuer key; no private signing key is
held by OneFoundry.

`grants` is a nonempty JSON list of distinct subject/client identities and actor
IDs, each with explicit project UUIDs. `company_read`, `capture` and
`company_capture` default false. Company capture requires both other grants.
OAuth requires `memory.read`; capture additionally requires `memory.capture`.
Tokens must have exact issuer/audience, subject/client, integer `iat`/`exp`, and
a positive lifetime bounded by `max_token_seconds` (default 900, range 60–3600).
An explicitly configured auxiliary OIDC audience is allowed alongside the
required resource audience; arbitrary additional audiences fail.

Bounds: results default 10 (1–20), deadline 10 seconds (2–60), in-flight 2 (1–8),
calls/minute/actor 30 (1–120), request bytes 131072 (4096–131072), token bytes 8192
(1024–16384). PostgreSQL statement/lock deadlines and bounded connection pooling
apply in the companion. Request cancellation waits for bounded database work;
retry captures with the unchanged idempotency ID to reconcile a lost response.
Failures return fixed reason codes and never raw driver inputs or tokens.

## Private cloud Work/Dot connection

The verified single-operator path is ChatGPT Work/Dot →
[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
→ the existing stdio bridge on the operator's machine → the key-authenticated
loopback MCP service → the same Postgres facts. Auth0 is optional. The native
Tunnel app uses **No authentication** at the MCP layer; its private organization,
workspace and app access control the cloud entrypoint. The bridge authenticates
locally with a separate read-only key and actor. This does not identify individual
OAuth users, and the cloud host receives neither the local key nor DB credentials.

For a new installation:

1. Obtain explicit operator approval for the exact organization/workspace,
   project/company grant, separate actor/key, private tunnel and persistent app
   connection. Any required schema upgrade or DB privilege change needs its own
   approved rollout and checkpoint. Confirm native Tunnels/developer-app access;
   do not accept purchases while preparing a connection.
2. Create the dedicated tunnel and restricted Tunnels Read + Use runtime key.
   Hand the key directly to an owned 0600 file through a native secure field or
   hidden terminal input. Preserve the existing Codex identity and give the cloud
   bridge its own expiring local key, with capture grants false.
3. Use a checksum-verified official tunnel-client and its typed YAML configuration.
   Configure one stdio `main` target running the existing bridge with the private
   client-file path. Keep health/admin on loopback, remote UI and raw/payload
   logging disabled, and REST/Harpoon targets empty. Run `doctor`, then the
   supported supervised `run --config <private-config>` service. Check health,
   readiness and successful control-plane polling. The HTTP/OAuth
   [tunnel sample](company-memory-tunnel.env.example) is for the optional OAuth
   route, not the stdio key bridge.
4. ChatGPT Plugins → Add → Create custom MCP server → Connection **Tunnel** →
   select the dedicated tunnel → **No authentication** → Create → Connect.
   Confirm the private app exposes exactly `memory_search` and `memory_read`.
   Select or mention it in a new Work/Dot context; an existing running agent may
   need a new context to load the new tool catalog.
5. Run bounded native search/read acceptance and verify content-free Gateway
   receipts for the cloud actor. Check foreign-project and unknown-fact denial,
   and absence of archive/history/review/delete/capture tools. Restart the service
   and repeat a native search. If the active project is empty, report the empty
   result and unavailable positive read; do not publish synthetic facts there.

The installed user services are enabled on user login. With user lingering
disabled, cloud access requires this machine to remain awake with its user session
running. Check health after host/network interruptions and renew/revoke credentials
through their owning private configuration. Conversation capture remains explicit
and subject to local operator review; connecting the app does not ingest chats.

Authenticated Streamable HTTP with OAuth discovery remains supported for clients
requiring individual OAuth identities or an approved direct HTTPS proxy. Configure
an approved HTTPS issuer/client with PKCE S256, RS256, exact audience/callback,
short token lifetime and exact subject/client grants. Complete the host's native
OAuth consent and real tool acceptance. That route has not been activated here.

The private portable plugin source is in `plugins/onefoundry-company-memory`.
`python -m scripts.package_memory_plugin --output <new-archive>` exports source
without an invented endpoint. `--verified-mcp-url` adds only a previously approved
and verified direct HTTPS connection. Native Tunnel connection is configured in
the host. A source ZIP or installed skill alone does not prove live Dot/Codex access.

## Local Nomic semantic retrieval (0.8.0)

`MEMORY_SEARCH_MODE=hybrid` adds exact cosine vector matching to the existing
word search. English [Nomic v1.5 GGUF](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF)
(Apache-2.0) runs through DistrictAI's native newline-JSON embedding protocol.
OneFoundry imports no DistrictAI application code and changes none of its services.
The approved native binary and model are external, checksum-pinned local assets;
no model download or network inference occurs during retrieval. Use
[the embedding sample](company-memory-embeddings.env.example). Explicit typed
settings override environment; no dotenv file loads automatically.

Schema `0072_memory_embeddings` adds a rebuildable vector cache in the **same**
Postgres database. Each vector names its owning project/actor, approved fact,
exact text hash, model/runtime hashes, prefix policy, dimensions and creation time.
Archives and pending candidates are excluded. Every search joins current facts
and their exact vector identity; withdrawal and supersession immediately exclude
old facts while retaining provenance. This is bounded exact search, suitable for
the single-operator store; it adds no pgvector extension or ANN index.

Nonempty hybrid queries require a complete index for the visible scope. Missing
vectors fail with `memory_embedding_index_incomplete`; model failures also fail
clearly. The facade exposes its existing `dependency_unavailable` reason. Empty
browse queries retain the previous newest-current-fact behavior. Word matches
rank first, then cosine similarity, newest creation time and ID. Similarity is a
ranking measure, never a confidence percentage. The API optionally returns
`similarity`; the UI marks zero-word matches as “Matched by meaning”. Existing
lexical responses stay compatible.

The native worker checks owned regular assets, hashes, model identity and vector
shape. CPU-only 2048-token context uses distinct `search_document: ` and
`search_query: ` prefixes, with truncation disabled. Inputs are bounded to 16
texts of 4096 UTF-8 bytes; query limits remain unchanged. One worker runs at a time
per application process, including queueing and loading within the configured
1–30 second deadline (default 5). Responses are bounded to 1 MiB by default
(4096–2097152 bytes); workers are reaped on completion, timeout and cancellation.
The default cosine floor is 0.5 (finite -1–1); tune it against authorized examples.
Context is configurable from 256–2048, dimensions 1–4000, task prefixes at most
64 characters. Changing weights, runtime, context, dimensions or prefixes changes
the cache identity and requires indexing again.

After the separately approved migration, grant the existing companion login only
`SELECT ON memory_embeddings`; the app/indexer uses its existing Tool Gateway
with SELECT/INSERT/UPDATE on this derived table. Keep external capture false.
Run `python -m onefoundry.memory_embedding_index --project-id <UUID> --limit 16`
in the approved operator environment, repeating until `indexed` is zero. Index
every project contributing visible company facts when enabling hybrid retrieval
or changing its model identity. The configured hybrid publication owner now indexes
new approvals and corrections in the same transaction as publication; restoring a
fact also fills any missing current-model vector. No partially indexed fact becomes
visible. A model failure rolls back the review or restoration, preserving the
pending candidate or withdrawn fact and any previous current fact. Rejection and
lexical-mode publication do not run inference. The existing local inference deadline
bounds the operation while its project lock is held. Indexing emits only content-free
Tool Gateway receipts; there is no separate background indexer to configure.

For the container app, the source-repository `compose.embeddings.yaml` overlay mounts only
the two explicitly configured files read-only at their configured absolute paths.
Both must be readable/executable as required by UID 10001, owned by that UID or
root, without group/other writers. The runtime image supplies libgomp/libstdc++.
The source host and companion can use their own approved asset paths with the
same hashes. No new inference listener or public network exposure is needed.
For an installed stack, put the following JSON in
`data/config/memory-assets.compose.json` under the install home (operator owned,
mode 0600). The operator launcher retains these mounts through start, restart,
backup and update; invalid, writable, foreign-owned or symlinked configuration
fails before Compose. This optional file permits exactly these two read-only
binds; arbitrary services or settings are rejected. The default remains lexical.
The configuration and external assets stay outside the checksummed release,
preserving the existing updater's archive contract.

```json
{"services":{"app":{"volumes":[
  {"type":"bind","source":"${MEMORY_EMBEDDING_RUNTIME_PATH:?Set an approved absolute local asset path}","target":"${MEMORY_EMBEDDING_RUNTIME_PATH:?Set an approved absolute local asset path}","read_only":true},
  {"type":"bind","source":"${MEMORY_EMBEDDING_MODEL_PATH:?Set an approved absolute local asset path}","target":"${MEMORY_EMBEDDING_MODEL_PATH:?Set an approved absolute local asset path}","read_only":true}
]}}}
```

Internal live text, conversations, chief and new team context use the configured
retrieval owner. Prepared team snapshots remain immutable. The explicitly granted
`approved_memory_v2` role tool uses configured retrieval; `approved_memory_v1`
retains its lexical contract. Neither tool grants new role/profile permissions.
Codex and cloud Work/Dot retain the existing two-tool connection and API keys.
After refreshing the installed companion or recreating its database container,
restart both `onefoundry-company-memory.service` and
`onefoundry-company-memory-tunnel.service` with `systemctl --user restart`.
Stopping the companion stops its dependent tunnel; starting the companion alone
does not start that tunnel. Verify its control-plane health and a native cloud
tool call after reconnecting.
Switching back to `MEMORY_SEARCH_MODE=lexical` reverses runtime activation without
deleting facts, archives or the derived cache. Live migration/grant/config changes
need their own approved checkpoint and rollback plan.
