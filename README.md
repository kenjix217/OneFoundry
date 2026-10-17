<div align="center">

# OneFoundry

**Your company. Your AI team. Your tools.**

A local workspace for AI specialists that share context, build reusable skills, and bring work back for your review.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Source: 0.20.0](https://img.shields.io/badge/Source-0.20.0-7465d8)](onefoundry/CHANGELOG.md)
[![GitHub stars](https://img.shields.io/github/stars/kenjix217/OneFoundry?style=flat&label=Star%20OneFoundry)](https://github.com/kenjix217/OneFoundry/stargazers)

[Quick start](#quick-start) · [Capabilities](#what-you-can-build) · [Python SDK](#build-your-first-extension) · [Architecture](#how-it-works) · [Contribute](CONTRIBUTING.md)

</div>

Give **Chief** an objective. Let **Researcher**, **Engineer**, **QA**, **GTM**, and **Sales** work from the same project context. Inspect their assignments, conversations, files, and results. Review useful knowledge into company memory so the next task starts with context.

Make the team your own: choose models, instructions, tools, memory scope, and procedures per profile, or add a custom specialist. Extend its capabilities with Python tools, API connections, MCP servers, webhooks, and versioned `SKILL.md` procedures through one Tool Gateway.

OneFoundry is **MIT licensed**, built for a **single operator on localhost**, and developed in the open. Source, architecture, configuration examples, SDK, and tests live in this repository.

## Quick start

You need a trusted **Linux host**, **Git**, and **Docker Engine with Compose v2**. The first build downloads dependencies and container images.

```sh
git clone https://github.com/kenjix217/OneFoundry.git
cd OneFoundry/onefoundry
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set **two different long passwords** for `DB_PASSWORD` and `APP_DB_PASSWORD`. Then:

```sh
docker compose up -d --build
curl -fsS http://127.0.0.1:8765/health/ready
```

Open **[localhost:8765](http://127.0.0.1:8765/)**:

1. **Create a project** and save its brief in **Settings → Project setup**.
2. Connect a supported model account in **Settings → AI connections**.
3. Choose models and review permissions in **Settings → Team**.
4. Give **Chief** an objective, or open **Integrations** to add a capability.

You can also choose **Continue without AI or a repository** and [record manual work](onefoundry/README.md#first-project-without-ai). Model usage may incur provider charges. Coding workers, mail, document accounts, and other optional services need their own configuration. See the [operator guide](onefoundry/README.md) and [environment example](onefoundry/.env.example).

## What you can build

| Capability | Put it to work |
| --- | --- |
| **A team for your company** | Chief coordinates specialist assignments and handoffs. Each profile has its own model, instructions, memory scope, tools, and autonomy setting. Talk to a specialist directly or follow its delegated conversation. |
| **Company memory that carries forward** | Review proposed facts into project or company scope. Later tasks retrieve approved knowledge with source references. Optional local embeddings and an authenticated company-memory MCP service extend the same library. |
| **Tools that fit your work** | Package typed Python functions, declarative API operations, MCP capabilities, resources, and procedures with the Python SDK. Keep reusable package definitions separate from each project's credentials and configuration. |
| **Skills made by you or your agents** | Write procedures in Tool Foundry or let an admitted agent propose a draft. Review exact instructions and required tools before accepting an immutable version. Pin versions to profiles. |
| **Connected APIs and MCP servers** | Save shared project connections, bind declared package slots, and configure them from a plugin, tool, or skill. Readiness shows configuration, credentials, live checks, and successful use separately. |
| **Event-driven work** | Receive signed, deduplicated webhooks through the existing automation pipeline. Prepare work from mail, watched files, or rules. Outbound webhooks and generic API/MCP calls keep exact-call approval and saved outcomes. |
| **Coding with independent verification** | The Execution Gateway records coding attempts in separate workers. The scoped maintenance path uses an isolated Codex candidate, independent QA, and operator-reviewed activation. Codex/Claude trusted-host fixtures have a separate setup and isolation contract. |
| **Research, drafts, and deliverables** | Work with input documents, approved memory, registered repositories, and granted public-web tools. Keep artifacts with their tasks. Optional document, mail, browser, and video workers add supported workflows. |

Try an objective such as **“Turn these product notes and customer findings into a launch brief and an outreach draft.”** Chief can assign Researcher, GTM, and Sales, retain their results, and bring the outward decision back to you.

## One place for integrations

**Integration Hub** and **Tool Foundry** are views over the existing Tool Gateway. They share its catalog, permissions, approval flow, and event records.

- **Connections** — configure project APIs/MCP endpoints and credentials, inspect readiness, and manage signed incoming webhooks.
- **Skills & Tools** — search tools, skills, and package versions; inspect prerequisites and bind saved project connections.
- **Tool Foundry** — create a skill proposal, import an inert ZIP, or start a Python/API/MCP/webhook package from a template. Review it before requesting activation.

![OneFoundry Integration Hub with a searchable catalog and versioned capability details](onefoundry/docs/assets/integration-hub.png)

*Actual application UI in the Black & Grey theme, using the synthetic account-brief SDK example. The preview shows an unconfigured worker; Python and local MCP execution require the separate supervisor.*

### Build your first extension

For SDK development, install **Python 3.12** and **uv 0.12.19**. From `onefoundry/`:

```sh
uv sync --locked
mkdir -p output
cp -R sdk/python/examples/account-brief output/account-brief
uv run --locked onefoundry-sdk validate output/account-brief
uv run --locked onefoundry-sdk test output/account-brief
uv run --locked onefoundry-sdk build output/account-brief --output output/account-brief.zip
uv run --locked onefoundry-sdk inspect output/account-brief.zip
```

This copies a complete example with an `extension.json`, Python source, a skill, and fixtures to edit. For an empty manifest, use `uv run --locked onefoundry-sdk init output/my-extension --id my-extension --name "My extension"`. `test` validates fixture structure and schemas; behavioral execution uses the configured isolated worker. These commands use the SDK from this checkout; they do not depend on a published PyPI release.

In **Integrations → Tool Foundry → Plugin package**, choose the ZIP and **Import inert draft**. Inspect its definitions, configure the shared project connections, and request activation of the exact package version. Review profile tool grants and skill pins separately in **Team**. A first-time connection template becomes available after its package draft is saved.

Start with the [SDK guide](onefoundry/sdk/python/README.md) or the [account-brief example](onefoundry/sdk/python/examples/account-brief/README.md), which combines a Python formatter, API/MCP lookups, a skill, and approval-bound webhook delivery.

**Bring existing procedures.** Native OneFoundry, Codex portable, Codex legacy, and Claude package adapters provide import/export and per-component compatibility reports. Supported skills, resources, provenance, and inactive remote MCP templates can travel. Foreign hooks, shell commands, agents, and permissions are rejected or remain inactive; import never executes them or transfers authority. See [format and runtime details](onefoundry/docs/UNIVERSAL_EXTENSION_RUNTIME.md#webhooks-and-portability).

## How it works

```mermaid
flowchart TD
    Operator["You: objectives and review"] --> UI["Chief workspace · Integration Hub · Tool Foundry"]
    UI --> Team["Chief and specialist model turns"]
    UI --> Gateway["One Tool Gateway: catalog, admission, approvals, events"]
    Team --> Gateway
    Gateway --> Context["Approved memory · sources · artifacts"]
    Gateway --> Connections["Approved API / MCP / webhook calls"]
    Gateway --> Worker["Separate isolated Python / local MCP worker"]
    Worker -->|"Declared connection callbacks"| Gateway
    Gateway --> Execution["Execution Gateway: exact coding attempts"]
    Execution --> Coding["Separate coding workers"]
    Coding --> QA["Independent QA and saved candidate evidence"]
    QA --> Review["Operator review before merge or activation"]
    Gateway --> Records[("PostgreSQL: projects, runs, approvals, events")]
```

**One authority for tools.** The Tool Gateway checks project scope, the run's saved permissions, current profile grants, exact inputs, and required approvals. Integration Hub does not introduce another dispatcher. A `SKILL.md` supplies procedure and never grants tools or credentials.

**Separate execution boundaries.** The app never imports arbitrary plugin Python or launches a coding CLI. Extension children run without network or host mounts, with a read-only root and resource bounds. Their declared connection requests return to the Gateway. The trusted supervisor is configured separately from the web app.

**Durable results.** PostgreSQL stores workflow state and append-only activity; artifacts have saved hashes. Runs pin package, tool, connection, binding, and worker identities. An uncertain external outcome stays unknown for reconciliation instead of being automatically sent again.

**Local storage, explicit connections.** Project data, artifacts, and private credentials use local storage. Configured model providers receive admitted task context; enabled connectors communicate with their configured services. OneFoundry is a localhost application with no public login or remote multi-user boundary.

The stack is **Python / FastAPI / Pydantic**, **OpenAI Agents SDK** for native model/tool turns, **React / TypeScript**, **PostgreSQL / Alembic**, and **Docker Compose**, with a locked `uv` environment. Coding providers retain their own agent loops. Read the [architecture](onefoundry/docs/ARCHITECTURE.md), [extension design](onefoundry/docs/UNIVERSAL_EXTENSION_DESIGN.md), and [supervisor contract](onefoundry/docs/EXTENSION_SUPERVISOR.md).

## Project status

**Early preview — source version 0.20.0.** The extension implementation includes the SDK, UI, API/MCP transports, isolated worker, reviewed skill drafts, webhooks, and package adapters. Source availability, installation, a connected account, and a qualified end-to-end workflow are separate states.

Recorded acceptance includes 20 SDK tests, 864 main unit cases, 673 selected PostgreSQL cases, 460 frontend tests, real worker/Gateway integration checks, and desktop/mobile/native 200% zoom checks. **The complete `make check` command is not green:** an existing Codex broker fixture exceeded a Docker attach deadline. The focused unchanged case passes, but that does not close the full regression gate. The [implementation record](onefoundry/docs/UNIVERSAL_EXTENSION_IMPLEMENTATION.md) preserves the failure and remaining work.

The [installed maintenance acceptance](onefoundry/docs/SELF_EXTENSION_COMPLETION.md) applies to its recorded release and configuration. Arbitrary repository changes and unrestricted deployment are outside that qualified path. Some account integrations still need live provider acceptance. This source update does not install or activate the capability on an existing system.

Use the [release guide](onefoundry/docs/RELEASE.md) for source bundles, installation, checksums, backups, health checks, and rollback. A public binary download channel has not been published.

## Find your way around

| I want to… | Start here |
| --- | --- |
| Configure and use the team | [Operator guide](onefoundry/README.md) |
| Build a plugin, tool, or skill | [Python SDK](onefoundry/sdk/python/README.md) · [Reference package](onefoundry/sdk/python/examples/account-brief/README.md) |
| Connect APIs/MCP or receive webhooks | [Extension runtime and settings](onefoundry/docs/UNIVERSAL_EXTENSION_RUNTIME.md) |
| Run isolated Python or local MCP | [Extension supervisor](onefoundry/docs/EXTENSION_SUPERVISOR.md) |
| Understand ownership and permissions | [Architecture](onefoundry/docs/ARCHITECTURE.md) · [Extension design](onefoundry/docs/UNIVERSAL_EXTENSION_DESIGN.md) |
| Work with company memory | [Memory guide](onefoundry/docs/company-memory.md) |
| Configure delegated coding | [Coding setup](onefoundry/README.md#delegated-coding) · [Maintenance runtime](onefoundry/docs/SELF_EXTENSION_RUNTIME.md) |
| Develop or report a problem | [Contributing](CONTRIBUTING.md) · [Issues](https://github.com/kenjix217/OneFoundry/issues) |
| See changes and verification | [Changelog](onefoundry/CHANGELOG.md) · [Progress](onefoundry/PROGRESS.md) |

```text
onefoundry/
├── src/onefoundry/  # API, workflow owners, Tool Gateway, Execution Gateway
├── frontend/       # React workspace and integration authoring UI
├── sdk/python/     # Extension contracts, package tools, and examples
├── runner/         # Separate execution and verification workers
├── migrations/     # PostgreSQL schema history
└── docs/           # Architecture, configuration, operations, and evidence
```

## Build with us

Found a workflow that should be easier? [Open an issue](https://github.com/kenjix217/OneFoundry/issues), share a reproducible bug, improve a guide, or contribute a focused change. Useful first contributions include SDK examples, connector adapters, accessibility, onboarding, and regression tests. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and review expectations.

If OneFoundry fits the way you want to work, **[give the project a star](https://github.com/kenjix217/OneFoundry)** to follow its progress. Help us make a company AI team easier to understand, extend, and operate.

README presentation draws on the onboarding and documentation structure of [OpenClaw](https://github.com/openclaw/openclaw/blob/main/README.md) and [Hermes Agent](https://github.com/NousResearch/hermes-agent/blob/main/README.md). The capability and architecture descriptions above refer to OneFoundry.

## License

OneFoundry is available under the **[MIT License](LICENSE)**. You can use, modify, and distribute it, including commercially, subject to the license notice. Third-party components retain their own licenses and notices. Package import/export preserves supplied provenance; it does not certify another package's license.
