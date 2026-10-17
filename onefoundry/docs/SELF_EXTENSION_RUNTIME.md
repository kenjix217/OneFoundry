# Self-extension runtime

The source implementation uses existing work orders, native team turns, assignments,
append-only Gateway events, the approval inbox, and company-memory review. It does
not start a coding CLI inside FastAPI. Software installation and live provider
acceptance are separate verification gates.

The installed runtime is 0.18.3, accepted on 2026-10-14. A native Codex
subscription attempt, independent QA, release preparation, approved activation,
capability readback, original-task continuation, company-memory publication and
later retrieval have passed live acceptance. The final source and image identities,
complete check matrix, recovery evidence and browser results are recorded in
[SELF_EXTENSION_COMPLETION.md](SELF_EXTENSION_COMPLETION.md). Existing credentials
and the original dirty checkout are preserved. Future access and installation
continue through the existing approval inbox.

Chief inspects the actual operation before assigning work. Existing denied,
disabled or unconfigured tools require setup. A verified implementation gap can
create one linked Engineer assignment. Chief can propose its maintenance target
in the conversation using `onefoundry.propose_maintenance_target`.

Configure the Engineer model and the intended role autonomy first. A manual
profile retains its operator-start behavior; approving source scope does not
silently change that policy. A confirmed coding failure blocks the linked
assignment and informs Chief once through the existing decision owner.

The target approval freezes one original task, one attempt, the selected source
revision, explicit edit paths, acceptance commands, a pinned QA image, the
Engineer model and reasoning setting, provider endpoint, execution bounds and output bounds. Changes to
these inputs require another concrete review. The running source, dirty checkout,
credentials and receipt storage remain outside the coding process.

## Separate coding worker

`python -m runner.repository_code_supervisor --config /PRIVATE/runner.json` validates
a mode-0600 JSON configuration, proves container isolation without an inference
request, publishes short-lived readiness, and consumes exact jobs once. The
configuration must match `RepositoryRunnerSettings`; required fields and optional
defaults are validated before startup. Credentials are a distinct mode-0600 file
owned by the supervisor user, outside both the source and receipt roots.

Use the existing external-spool bind and private shared group for the container
app and trusted host supervisors. Prepare the spool and its four directories with
mode `2770`, and add that group to the app through the existing Compose contract.
Jobs and evidence use mode `0640`; configuration and credentials remain `0600`.
Only trusted supervisors share this channel. Coding containers never mount it.

Configure pinned `cli_image` and `broker_image`, exact container executable paths,
the configured Engineer `model_id`, an HTTPS Responses `provider_url`, broker port,
request timeout, polling and readiness intervals, container resource bounds,
`spool`, `source_root`, `credential_file` and `policy`. This bridge supports Codex's
Responses API credential route by default (`auth_kind=api_key`). With the explicit
`codex_subscription` kind, `credential_file` names the existing owned mode-0600
Codex login file and `provider_url` must be the exact Codex subscription Responses
endpoint. Only the current access token and account ID reach the private broker;
the login file and refresh token never enter either container. Expired or invalid
login fails before coding; the adapter does not refresh, overwrite login, or fall
back to a paid API key. The subscription protocol does not accept an output-token
cap; request counts, time, request/response bytes and local output remain bounded.
Set optional `qa_image` to the pinned independent QA image to advertise it to Chief.

The worker JSON supports `provider_overload_retries` (default 1, integer 0–2) and
`provider_overload_retry_delay_seconds` (default 1, number 0.1–30). These settings
come only from the locked worker JSON; explicit values override their defaults,
with no environment override. For example, the optional configuration fragment is
`{"provider_overload_retries":1,"provider_overload_retry_delay_seconds":1}`.
Only a sole structured `server_is_overloaded` rejection, before any response is
delivered to the coding child, can retry. Every retry consumes the existing exact
request budget and remains inside the CLI deadline. Set retries to 0 to disable it.
Transport loss, missing/incomplete terminals, rate limits and unknown errors never
retry. Safe retry counters and allowlisted rejection codes remain in diagnostics;
raw provider error text does not. The stopped coding attempt itself is never replayed.


Codex receives a filtered committed source view and a disposable private home.
Git history/configuration, private paths, binary/oversized files and credential
literals stay outside that view. Protected edit paths fail before provider launch.
Only approved text changes are copied back; deletions, symlinks, hardlinks and
changes outside the scope are checked. Configured acceptance checks must fail on
the baseline and pass on the candidate. Independent QA reconstructs the exact saved
patch in a fresh tree and runs the same approved commands without public networking.

Build the QA dependency image with `docker build --target repository-qa .` and pin
its resulting SHA-256 ID. It imports the mounted candidate through `/workspace`,
with dependencies outside `/app`. Python checks can use `python -m pytest`; the
image disables bytecode and pytest caches. Commands needing databases or additional
services require an isolated test setup; passing unit checks does not replace them.

## Configuration

`REPOSITORY_CODE_POLICY` is typed JSON. Omitted uses defaults; `{}` also selects
defaults. Explicit constructor values override environment, then defaults. Empty,
invalid and unknown fields fail validation. Python and Compose use the same owner.

| Field | Default | Allowed |
| --- | ---: | --- |
| max_files | 10000 | 1–100000 |
| max_bytes | 67108864 | 1024–268435456 |
| max_tree_bytes | 4194304 | 1024–16777216 |
| stage_seconds | 30 | 1–120 |
| wall_seconds | 900 | 30–3600 |
| receipt_wait_seconds | 1800 | 120–7200; at least wall + stage + 60 |
| max_context_file_bytes | 262144 | 1024–1048576 |
| max_provider_requests | 80 | 1–100 |
| max_provider_request_bytes | 1000000 | 1024–4000000 |
| max_provider_response_bytes | 4000000 | 1024–16000000 |
| max_provider_output_tokens | 16000 | 1–64000 per request |
| maintenance_lock_seconds | 10 | 1–30 |

Existing `RUNNER_MAX_LOG_BYTES` and `RUNNER_MAX_DIFF_BYTES` remain the output owners.
The immutable manifest's receipt timeout is used for reconciliation. Request and
token bounds are not a dollar-cost guarantee. Unknown provider outcomes remain
unknown and block automatic replay.

## Review, release and resumption

A candidate is source evidence. Independent QA must report its observed receipt.
Chief invokes `onefoundry.prepare_extension_release` only after confirmed independent
QA. `python -m runner.extension_release_worker --config /PRIVATE/release.json`
consumes the request, reconstructs the exact candidate, and invokes the existing
versioned release builder. It rejects changes to build programs and dependency
graphs requiring a different trusted build environment. The separate
`runner.extension_release_record.record_release` verifies that exact archive,
candidate and QA before Chief can propose activation. Source merging and release
preparation uses the operator-configured host worker; activation still requires
the exact approval. Configure `preparation.managed_source_alias` for the dedicated maintenance clone
to synchronize it with approved activation and rollback. A fresh named source
offer then exposes the installed candidate for the next scope proposal.

The activation inbox binds candidate, diff, QA, package SHA-256, version/image,
expected installed base, configuration/migration effects and rollback plan. A new
tool's exact original-profile grant is included in that same human decision.
`runner.extension_release_supervisor` consumes one approved job using configured
install/archive roots and localhost origin. It acquires a maintenance lease, drains
writes and managed workers, invokes the existing update/checkpoint owner, applies
only approved role grants, and verifies installed identity plus capability. Failed
readback invokes rollback. Unknown activation retains the lease for reconciliation.
Read-only status remains available throughout maintenance.

One confirmed activation cause restores one original checkpoint through the
existing follow-up owner. Current permissions and owner readiness are rechecked.
The original deliverable still needs verification. Company knowledge uses admitted,
cited source reads, human-approved draft capture, existing memory publication and
later retrieval. Conversation capture is still off by default.

Codex custom-provider contract: [official documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers).

The request allowance includes planning, file inspection, edits and test-fix turns.
The isolated broker retains bounded failure codes and request counts in the session
observation. A request-limit failure requires a newly approved attempt after its
disposable work is explicitly discarded; changing configuration never replays it.

An expired host attach deadline is recorded as `maintenance_cli_timeout`; without
a terminal provider receipt its outcome remains unknown. A larger maintenance
task can use an explicitly configured longer `wall_seconds` and matching
`receipt_wait_seconds`, frozen into a fresh approval after the stopped disposable
work is discarded. Increasing the window does not resume or approve the old job.

## Superseded checkpoints

A later installed extension can satisfy the same original capability checkpoint.
Canceled work that never launched, and completed or canceled candidates whose activation
proposals were all rejected before any installation was queued, retain their
history without blocking that verified replacement. Active or unknown coding,
nonrejected activation proposals and queued installation remain blocking. A
different project, task, capability, source or revision cannot retire the old
checkpoint. No old attempt is relabeled successful.
