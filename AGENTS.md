# OneFoundry instructions

OneFoundry is a localhost AI team for one company operator. Start with Engineer, QA, Researcher, GTM, and Sales; add Infra later. The operator can create or add custom agent profiles. Each role/profile sets a model, instructions, memory scope, allowed tools, and autonomy level. Assign work orders to a role/profile; allow handoffs (Engineer to QA; Researcher to GTM to Sales). All roles share company memory, filtered by scope. A `SKILL.md` gives procedure but never grants tools or permissions.

## System

- Build Python `onefoundry` with FastAPI, Pydantic, strict type checking in CI, `uv` and a lockfile; React/TypeScript; PostgreSQL; Docker Compose. Single user, localhost, no login or new Go/Rust runtime.
- Fresh tables: projects, roles/profiles, work orders, runs, results, artifacts, memory, events. Every row records project ID and operator/agent actor ID. Work order states: `draft -> ready -> running -> done / failed / unknown`; allow approval waits.
- One Python Tool Gateway mediates OneFoundry-owned agent actions. A separate Execution Gateway binds Engineer work orders to external coding providers and records exact attempts, sessions, and receipts. OneFoundry never implements its own coding-agent loop or runs coding-agent commands in the app process. A separate runner uses disposable repositories and bounded execution; OS isolation from receipt storage and credentials, plus provider-only network access, must be proved before live CLI launch.
- Keep artifacts on a persistent local volume, with paths and SHA-256 hashes in PostgreSQL. Store credentials in an OS keyring or permission-locked file. Step 4 adapted selected behavior from historical KJ31 workers; the active app must not import the local archive.

## Actions and activity

Use a simple approval inbox, without heavy governance. Email, publishing, merging, deploying, cloud changes, and other outbound actions wait for operator approve/reject, recorded in events. Role agents may send company data to the configured AI provider; the Researcher may use web search. Nothing else leaves without approval. Secrets never go to models, logs, events, memory, or artifacts.

Out of scope: compliance audit, multitenancy, JintellarCore import, and provider attestor/review-byte machinery.

Only the Tool Gateway and runner write append-only events; a PostgreSQL trigger blocks UPDATE/DELETE. Record task, instructions, model, action times/results, diff, tests, token use, cost, final outcome, reason. Mask secrets before storage. Conversation capture is off by default. Show timelines and search; optional tracing never replaces events.

## Build order

1. Skeleton, Compose, schema, health.
2. Projects + roles, including create/add custom agent profiles.
3. Work orders, handoffs, events, run timeline, approval inbox.
4. Memory + DreamCycle behavior adapted into the active Python package.
5. Researcher + GTM roles, text only, for first real value.
6. Engineer + QA with the sandboxed runner.
7. Sales drafts + approval, then Infra with read-only monitoring first.
8. Review Go removal.

Before step 3, compare two agent frameworks for roles/handoffs and recommend one. Write no custom agent loop without the owner's OK. Work only on the approved step; commit, test behavior, and give repeatable browser clicks/results. Keep docs short; ask before extras.

The Git-ignored local Go archive at `legacy/kj31/api/` stays frozen at `go-freeze` until Python covers used behavior and the owner approves permanent removal. Archived docs are reference only. Codex must not edit `/home/ken/districtAI`; later only OneFoundry's Engineer works there on branches, with inbox-approved merges. Do not change local Codex settings/authentication without approval. Before open source: fresh public repo, gitleaks, MIT or Apache-2.0; not now.

**Current direction:** the original numbered build slices reached the offline pilot. Add the Execution Gateway so Codex CLI, Claude Code, and later coding systems do the coding while OneFoundry owns work orders, exact launch records, reconciliation, QA evidence, and approval-bound merges. Keep each slice independently testable and committed, and record checks in `onefoundry/PROGRESS.md`. Do not present an installed CLI or text-model account as a proved live coding connection.

## Codex development model policy

For Codex development sessions, follow the `MODEL AND REASONING POLICY` in
`~/.codex/AGENTS.md`. OneFoundry product role/profile models remain configured
per role/profile through the application's configuration system.

## Engineering working agreement

The following rules supplement the OneFoundry requirements above. Preserve
OneFoundry's architecture, approved build scope, operator approval flow,
append-only event ownership, frozen archive, and gateway/runner boundaries.

### Development checkout

Use the primary OneFoundry checkout on `main` for regular development. Keep one
development worktree; create another only when the operator explicitly requests
or approves it. Preserve uncommitted work when consolidating branches. Runner-owned
disposable repositories continue to follow the existing execution-isolation contract.

### Coding style

- Follow the active package's formatter, linter, naming, and type-checking
  conventions. Use explicit Python types and Pydantic boundary models, and keep
  TypeScript types consistent with the existing API contracts.
- Keep imports and dependencies within their owning package. Prefer explicit
  domain names and small cohesive functions over implicit behavior.
- Format only files changed for the task; preserve unrelated work.

## 1. Investigation Before Modification

For bug fixes and incidents, begin read-only.

Before changing code:

- Reproduce the symptom whenever reasonably possible.
- Identify the owning component, failed contract, failed sink, and runtime path.
- Inspect logs, configuration, data state, dependency behavior, and environment
  before concluding that source code is defective.
- State the leading root-cause hypothesis, supporting evidence, competing
  hypotheses, and confidence.
- Do not implement a fix until root-cause confidence is at least 80%.
- An 80% confidence statement is invalid without direct evidence such as a
  reproduction, failing regression test, owner-path trace, controlled
  experiment, stack trace, database evidence, or dependency-contract mismatch.
- If confidence remains below 80%, continue diagnosis and report the uncertainty.
- For destructive, security-sensitive, schema, project/memory-scope, or deployment changes,
  require stronger evidence and explicit authorization.
- HTTP success, dispatch success, compilation, or source inspection alone does
  not prove semantic completion.

## 2. Modular Design

- One module or file owns one cohesive responsibility.
- One function performs one logical operation or transformation.
- Multiple tightly related private functions may remain in the same file.
- Split a function when it mixes concerns such as validation, orchestration,
  I/O, persistence, transformation, policy, and presentation.
- Keep entrypoints, handlers, adapters, routers, and public wrappers thin.
- Put implementation logic in a clearly named domain-owned module.
- Do not create giant helpers, god objects, generic managers, or dumping-ground
  modules named `utils`, `helpers`, `common`, or `misc`.
- Shared code must have a real stable abstraction and at least two legitimate
  consumers; do not generalize speculative reuse.
- Prefer adding a focused module over extending an already oversized file.
- Do not refactor unrelated legacy code merely because it is nearby.
- Do not duplicate an existing owner or bypass a public component contract.
- Do not reformat or restructure vendored, generated, or submodule code unless
  that code is explicitly in scope.

## 3. Contracts and Errors

- Validate untrusted input at the system boundary.
- Use typed or structured errors with actionable context.
- Never swallow errors, convert failure into empty success, or add a fallback
  that hides the real owner failure.
- Preserve error identity so callers can distinguish retryable, invalid-input,
  authorization, dependency, and internal failures.
- Make timeouts, cancellation, retries, and resource bounds explicit.
- Retries must be bounded, observable, and safe for the operation's idempotency.
- Configuration and deployment-specific values belong in config or environment,
  not hardcoded source.
- Preserve backward compatibility unless a breaking change is explicitly
  authorized and accompanied by a migration path.

## 4. Testing and Completion

For every code, API, configuration, schema, dependency, build, packaging, or
runtime behavior change:

1. Add or update a regression test that represents the defect or new behavior.
2. When practical, prove the regression test fails before the fix.
3. Run focused tests while iterating.
4. Run the repository-defined full regression suite before completion.
5. Run the affected integration and end-to-end paths against real services.
6. For user-visible changes, run real browser acceptance at the required
   desktop and mobile viewports.
7. Run concurrency, race, sanitizer, or isolation checks when the change affects
   those concerns.

A skipped, unavailable, quarantined, or baseline-accepted test is not a pass.
If required services, data, credentials, browsers, or infrastructure are
unavailable, report the result as partial or blocked and identify every
unexecuted gate.

Docs-only, comments-only, and test-only changes may skip the full runtime suite
unless repository-specific instructions require it.

Tests must cover:

- Normal success.
- Invalid or malformed input.
- Real dependency or owner failure.
- Boundary and empty cases.
- Authorization, project/role isolation, and memory/tool scopes where applicable.
- Cancellation, timeout, and concurrency behavior where applicable.
- Backward compatibility of externally visible contracts.

Avoid nondeterministic sleeps. Inject clocks, randomness, dependencies, and
external clients when needed for deterministic tests.

## 5. Security and Data

- Never place credentials, tokens, secrets, or protected data in source,
  fixtures, logs, URLs, metrics, traces, screenshots, or generated artifacts.
- Preserve operator approvals, authorization, project/role isolation,
  memory/tool scopes, path validation, and provider/network boundaries.
- Treat all external data as untrusted.
- Use parameterized database queries.
- Do not weaken a security boundary to make a test pass.
- Check compatibility and license before adding a dependency.
- Do not add dependencies solely to avoid writing a small focused implementation.

## 6. Delivery

- Preserve unrelated uncommitted work.
- Review the complete diff before declaring completion.
- Run formatter, static analysis, compilation, tests, and `git diff --check`.
- Update documentation when externally observable behavior changes.
- Do not commit, push, deploy, migrate, publish, or modify production unless
  explicitly requested.
- Report exactly what was verified, what was skipped, and what remains uncertain.

## 7. Configuration and Runtime Values

- Runtime policy and deployment-specific values must come from the
  repository's configuration system, environment, or credential store.
- Do not hardcode model names, providers, endpoints, routing preferences,
  project IDs, role/profile settings, scopes, service URLs, ports, socket
  paths, table names, storage roots, feature flags, limits, timeouts, retry
  policies, credentials, or secrets.
- Use the project's existing configuration owner or typed settings module.
  Do not scatter direct environment-variable reads throughout domain logic.
- Configuration precedence must be explicit and deterministic. Follow the
  repository-defined order; do not invent a new override hierarchy.
- Validate configuration at startup or the earliest owning boundary.
- Missing or invalid security, credential, storage, gateway, or runner
  configuration must fail clearly and safely. Do not silently substitute
  permissive defaults for the approved configuration.
- Defaults must be safe, documented, and independent of one developer's
  machine or filesystem.
- Secrets belong in environment variables or the approved credential store.
  Secrets must never appear in committed config files, YAML, source code,
  fixtures, logs, metrics, traces, URLs, browser state, client bundles,
  screenshots, generated documentation, or artifacts.
- Before adding a new setting, confirm that an existing setting or config
  owner does not already cover the requirement.
- Every new or changed setting must include:
  - schema or type validation;
  - valid, missing, and invalid-value tests;
  - environment/config precedence tests when overrides are supported;
  - documentation in the repository's environment or configuration reference;
  - updates to sample configuration without including real secrets.
- Configuration changes that alter runtime behavior require the same targeted,
  regression, integration, and E2E verification as source-code changes.
