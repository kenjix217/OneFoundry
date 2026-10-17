# Contributing to OneFoundry

OneFoundry is an MIT-licensed, single-operator localhost AI workspace. Contributions to tools, SDK examples, integrations, accessibility, documentation, and reproducible fixes are welcome.

## Start with a workflow

For a bug, include the source version, expected and observed behavior, repeatable steps, and a minimal sanitized example. State whether the issue occurs in a source checkout or an installed release. For a larger capability, open an issue describing who needs it and the existing owner it extends.

Use the [issue tracker](https://github.com/kenjix217/OneFoundry/issues) for public discussion. Keep credentials, account identifiers, company data, and unredacted provider logs out of issues and pull requests.

## Development setup

Use Linux, Git, Docker Engine with Compose v2, Python 3.12, uv 0.12.19, and Node.js 22 with npm. See the [operator guide](onefoundry/README.md) for application startup.

```sh
git clone https://github.com/kenjix217/OneFoundry.git
cd OneFoundry/onefoundry
uv sync --locked
npm --prefix frontend ci
```

The dependency lockfiles are committed. Keep model credentials and runtime values in the documented configuration owners; use the safe environment samples rather than committing a live `.env`.

## Choose the owning component

| Change | Owner |
| --- | --- |
| Package definitions, tool admission, approvals, connection dispatch | Existing Python Tool Gateway and `gateway_extension_*` modules |
| Reusable package validation, formats, fixture schemas, authoring commands | `onefoundry/sdk/python/` |
| Integration Hub and Tool Foundry views | `onefoundry/frontend/src/ExtensionHub.tsx` and its API/styles |
| Isolated Python/local MCP execution | Separate extension supervisor and worker modules under `onefoundry/runner/` |
| Coding providers and exact attempts | Execution Gateway and separate coding runners |
| Approved company facts and retrieval | Existing memory owners |

Read [AGENTS.md](AGENTS.md) and the [architecture](onefoundry/docs/ARCHITECTURE.md). A skill supplies instructions; it never grants tools, credentials, or permission. The app does not execute untrusted package Python or host a coding-agent loop. Preserve project scope, saved/current permission checks, exact approvals, append-only events, and uncertain-outcome recovery.

## Verify a change

Add a meaningful regression for new behavior or a reproduced defect, then run focused checks while iterating. Format only the files you changed. From the repository root, run the product gate:

```sh
cd onefoundry
make check
```

This checks the SDK, formatting, lint, strict Python types, unit tests, real Docker isolation, disposable PostgreSQL integration, and frontend lint/tests/types/build. Docker and local fixture images are required. See [extension verification](onefoundry/docs/UNIVERSAL_EXTENSION_RUNTIME.md#verification) for the browser and QA image prerequisites. Disposable test databases are separate from an installed app.

Run affected real-service paths and browser checks for user-visible changes, including desktop, mobile, keyboard, and native 200% zoom. A mocked response or structural SDK fixture check is not proof of a live connector or isolated execution. Report every skipped, failed, or unavailable gate; do not increase limits or bypass isolation to obtain a pass. Docs-only changes can use link, example, and rendering checks.

## Keep publication safe

From the repository root, set up the pinned scanner and hooks:

```sh
python3 onefoundry/scripts/install_gitleaks.py .git/tools/gitleaks
git config --local core.hooksPath .githooks
```

The hooks reject private runtime files, embedded repositories, non-example addresses, unsuitable Git identities, and secret findings. Use your verified GitHub no-reply commit identity. History is checked as well as the current tree; do not bypass a failed hook. See [Git publication safeguards](onefoundry/docs/GIT_HYGIENE.md).

Keep generated archives, live configuration, databases, local repositories, and test output out of commits. Public documentation screenshots belong under `onefoundry/docs/assets/` and must use synthetic data. Supplied third-party license and notice text must remain intact.

## Send a focused pull request

Explain the concrete problem, resulting behavior, relevant owner and contract changes, and checks performed. Include screenshots when they help assess a UI change. Keep unrelated work out of the diff. Update the user-facing guide and changelog when behavior changes, and follow the repository's version rules for product changes.

Publishing source does not install it, migrate an installed database, or activate a provider. Those operations retain their own setup, approvals, and verification.
