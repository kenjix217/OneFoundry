# OneFoundry self-extension

**Status: accepted and installed in 0.18.3 on 2026-10-14.**
The native workflow inspected capabilities, recovered approved source setup,
commissioned a Codex subscription coding attempt, obtained independent QA,
prepared and activated an approved package, and resumed the original repository
review. The original task is now complete; cited company knowledge is published
and was retrieved by a later task. Final acceptance includes current-version
readback, all required installed flags/workers, failure recovery, the complete
regression matrix, and desktop/mobile browser verification. Exact evidence and
remaining product boundaries are in the [acceptance record](SELF_EXTENSION_COMPLETION.md)
and [runtime setup](SELF_EXTENSION_RUNTIME.md).

## Outcome

When an operator request needs a capability OneFoundry lacks, Chief establishes
the exact gap, commissions Engineer through the Execution Gateway, obtains
independent QA, prepares an approval-bound activation, and resumes the original
request after observing that the capability works. The operator supplies direction
and makes meaningful access/release decisions; Chief manages specialist work.

Improvement is driven by a real task and an observable acceptance check. A new
feature is useful only when it enables the original requested outcome. Keep the
existing SDK agent execution and separate coding providers; add no custom coding
agent loop or shell execution inside the app.

## Historical starting point before this implementation

- The repository owns bounded committed-source reads in `role_repository_file.py`,
  admitted via `role_tool_admission.py` and native `task_session_tool_readiness.py`.
  Project selection, exact revision, protected paths, sensitive text, output limits
  and saved/current profile permissions still apply.
- `task_execution_capabilities.py` now projects typed, scoped blocker checks.
  The bounded repository owner checks source identity/revision without reading
  file content, and Execution Gateway checks its fresh runner report. The initial
  inventory is described in `CAPABILITY_READINESS.md`; it is not successful execution.
- `task_session_tools.py` supports Chief delegation and saved observations. A saved
  assignment is not a coding launch or an accepted deliverable.
- `execution_gateway.py` records external attempts, sessions, artifacts and
  reconciled receipts. The new `repository_code_v1` target requires exact source,
  task scope, paths, checks, model and execution bounds in an operator approval.
  The older trusted-host fixture does not satisfy this target's isolation checks.
- Installed 0.13.0 initially had both requested flags false, an empty source root,
  no repository bind overlay, and no shared bind to the active host CLI worker.
  The host worker is active, but its trusted-local mode does not prove OS isolation.
- Company-memory reads and reviewed capture already have owners. An artifact
  containing facts is not a published company-memory entry.

## Workflow and ownership

| Stage | Owner and input | Persisted output and completion condition |
| --- | --- | --- |
| Inspect | Chief uses a Gateway-projected inventory for the original project, profile and task | Exact capability status, owner failure, inspected revision and observed evidence |
| Resolve setup | Existing configuration/source/profile/connection owners and separate host setup worker | Chief prepares one precise setup decision, the owning service applies approved effects, verifies the capability and resumes the task |
| Request extension | Chief creates an idempotent linked Engineer work order through existing assignment owners | Gap, original objective, allowed scope, expected behavior, acceptance checks and configured budget |
| Build | Execution Gateway and separate runner consume the pinned OneFoundry repository snapshot | Candidate branch/commit, actual diff, baseline/final tests, provider session and reconciled receipt |
| Review | Independent QA consumes candidate bytes and tests the original use case | Exact candidate-bound verdict, regression/isolation evidence and remaining limitations |
| Prepare release | A separate release worker uses the existing release builder on the exact QA-reviewed candidate | Verified archive, version, image, migration/configuration effects and rollback proposal, ready for operator review |
| Activate (live acceptance passed) | Existing approval inbox plus a dedicated release supervisor consume an exact approved candidate | Installed version/image/configuration, health and capability readback, reversible checkpoint |
| Resume | Existing task follow-up owner consumes confirmed activation evidence | One resumed original task, original deliverable verified, sources and memory readback where applicable |

### 1. Classify before building

Extend the existing capability projection from canonical tool registrations and
owner readiness checks. Report `ready`, `disabled`, `permission_required`,
`configuration_missing`, `dependency_unavailable`, `missing_implementation` or
`unknown`. Include the operation, owning component, safe error/reason, observation
time, source revision and permitted next action. Do not infer readiness from tool
names, CLI installation, repository metadata or model prose.
Return every independently verified blocker and its owner when several prerequisites
are missing; one enabled flag cannot hide an absent mount, grant or runner.

Only a verified `missing_implementation` creates extension work. Existing
capabilities with absent access/configuration get a concrete setup proposal.
Unknown effects require reconciliation; transient dependency failures use their
existing bounded retry policy. An agent cannot turn a denied tool into a missing
feature in order to bypass authorization.

### 2. Bind an extension to the original task

Reuse work orders, assignments, sessions and Gateway receipts. Store an explicit
gap-to-original-task link and checkpoint; avoid a competing workflow database.
The request includes the exact inspected revision, contract to implement, allowed
files/side effects, baseline reproduction, success/failure checks and return task.
Deduplicate the same gap/revision within one original task and approved pilot.
Other original tasks require their own explicit maintenance approval.
Use typed Pydantic request/response contracts; obtain actor and scope server-side.

Implementation must first establish an owner-path reproduction and at least 80%
root-cause confidence. Features need a failing acceptance check before a candidate.
New access, credentials, migrations, spending and policy changes retain explicit
operator authority. A future standing policy may authorize bounded internal
development; this proposal does not establish that policy.

### 3. Make OneFoundry a supported coding target

Add an explicitly configured target to the existing Execution Gateway/runner,
with a pinned approved OneFoundry commit and disposable checkout. Do not reuse
the fixture selector as arbitrary repository access. Prove OS separation from
receipt storage/credentials and provider-only egress before live coding launch.
Keep candidate output separate from running application code and persistent data.

Keep extension work in the original project. OneFoundry must be an explicitly
registered, approved maintenance source/target for that project; selecting DistrictAI
does not grant access to OneFoundry. Reject foreign-project targets and wildcard
repository grants. Request this precise setup when the maintenance target is absent.

The runner supplies observed command/test results and immutable artifact hashes.
Cancellation and timeouts fence the attempt; uncertain provider outcomes remain
`unknown` and block automatic re-launch. Existing runtime/role model, timeout,
assignment, no-progress and budget settings remain owned by configuration. Any
new setting requires validation, precedence tests and sample/reference updates.

### 4. Review and activate the exact candidate

QA must independently verify the actual candidate, test the original workflow,
and cover scope denial, failure, empty/boundary cases and affected concurrency.
Engineer cannot approve its own work or alter QA criteria to pass. Corrections
receive a fresh candidate identity and independent review.

Bind activation approval to project, source revision, candidate commit, diff,
tests, packaged version/image, configuration/migration effects and rollback plan.
Merge and local installation/restart remain explicit approved actions. A separate
supervisor performs activation; the app does not overwrite its running code.
Health success is only one check: verify the activated tool through its real owner
and original workflow before reporting that the missing capability is available.

Approval rules, secret handling, project/profile boundaries, append-only events
and runner permissions are protected contracts. Changes to these require separate
explicit authorization and review. A skill describes procedure and grants no tools.

### 5. Resume and retain evidence

Persist the original task, checkpoint, candidate/activation identity and last
confirmed effects before yielding. One activation cause queues one resumed turn
through the existing follow-up owner, with idempotency and expected-version checks.
Restart/replay must not duplicate assignments, provider attempts or memory writes.
Rejected activation leaves the original task waiting with a clear reason.
Failed activation restores the checkpoint and preserves all attempts/evidence.

Company knowledge becomes a bounded source-backed draft through existing memory
owners, then operator review, publication and subsequent retrieval verification.
Do not grant a specialist unrestricted published-fact writes. Proven procedures
may become reusable skills under existing scope/review policy.

## Operator experience

Use the original conversation: "Repository reading needs setup", "Engineer is
adding the missing reader", "QA verified the original request", and a concrete
activation decision. Resume there after activation. Put exact IDs, flags, receipts,
diffs and earlier attempts in Details. Avoid a new dashboard or asking the operator
to choose specialists, start each child, or manually replay the original request.

## Implementation slices and acceptance

1. **Capability defaults (this change).** Default both flags to true in Python,
   Compose and the environment sample. Use the existing `/source-roots/workspace`
   container directory as the source default. No host path is mounted and no
   profile permission is granted by a default. Explicit false overrides remain
   supported. Explicit empty/invalid source configuration fails while reads are
   enabled. Verify existing repository/coding denials and readiness failures.
2. **Inspection and setup recovery (accepted live).** Scoped capability observations
   distinguish absent implementation from missing configuration/access. Native setup
   proposals were approved, applied, verified against committed content for the
   intended profiles, and followed by continuation of the same task.
3. **Coding target (accepted live).** The separate isolated worker used the existing
   Codex subscription on the approved OneFoundry target and produced a saved exact
   candidate. Disposable source, provider-only egress and credential/receipt separation
   remain enforced. Failed and unknown attempts are preserved.
4. **QA and activation (accepted live).** Independent QA reconstructed and tested the
   saved candidate. The native release worker packaged it and installed its exact
   approved archive with checkpoint and capability readback. A deliberate failed
   update restored the prior image and data; restart did not duplicate activation.
5. **Resumption and knowledge (accepted live).** Engineer, Researcher and independent
   QA completed committed repository review, including complete large-file reads.
   Reviewed company facts were published and retrieved in a later task. The original
   task reached native completion. Automatic hybrid-memory indexing and replacement
   of terminal, never-activated extension requests are covered by the final repairs.

Each code slice requires focused regression tests, `make check`, real owner-path
integration and desktop 1280x900/mobile 390x844 browser acceptance. Skipped or
unavailable gates are recorded as partial, never passed. Commit/install each slice
only with the corresponding operator authorization and record it in `PROGRESS.md`.

## First end-to-end demonstration

Use the operator's original request: inspect an explicitly selected repository,
explain architecture from exact committed files, gather cited documentation, draft
company knowledge for review, and retrieve the approved facts in a later task.
Use a disposable missing-capability scenario to demonstrate engineering extension
without manufacturing a defect in the live repository.

Completion requires source citations, a useful architecture result, a verified
candidate where necessary, independent QA, approved activation/readback, resumed
original work and actual company-memory retrieval. Messages alone establish none
of those outcomes.
