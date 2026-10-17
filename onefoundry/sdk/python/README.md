# OneFoundry Python SDK

Part of the [MIT-licensed OneFoundry project](../../../LICENSE).

`onefoundry-sdk` defines inert authoring contracts for local OneFoundry extensions. A package describes tools, connection slots, and instruction skills. The SDK validates and hashes package contents; importing the SDK or inspecting a ZIP never runs packaged Python. OneFoundry's Tool Gateway remains the only authority for agent tool admission, operator approvals, and durable events.

```sh
uv run --locked onefoundry-sdk init /tmp/example-extension --id example-extension --name "Example extension"
uv run --locked onefoundry-sdk validate /tmp/example-extension
uv run --locked onefoundry-sdk test /tmp/example-extension
uv run --locked onefoundry-sdk build /tmp/example-extension --output /tmp/example-extension.zip
uv run --locked onefoundry-sdk inspect /tmp/example-extension.zip
```

`test` checks fixture structure, tool references, and input/expected-output JSON Schemas without executing code. For behavioral fixture execution on a trusted host, use `python -m runner.extension_fixture_test --config PRIVATE_WORKER_CONFIG.json --archive PACKAGE.zip` from the OneFoundry application package. This requires the host supervisor's private policy file and a pinned local worker image. It runs only declared pure Python fixtures through the network-disabled, mount-free worker and compares exact JSON output; connection-backed tests require the Tool Gateway's separate admission and approval path. A successful SDK build or structural test is not evidence of a live connection or tool result.

The [account-brief reference package](examples/account-brief/README.md) combines a skill, pure Python formatter, HTTP lookup, MCP lookup and approval-bound webhook delivery. Its connection slots contain no endpoint or credentials. The application's required isolation gate builds the current worker and exercises this same package through real Gateway approvals, disposable services and signed inbound automation.

The root `extension.json` uses `schema_version: 1`, a lowercase slug `id`, SemVer `version`, and optional arrays of tools, connections, and skills. Tools declare JSON Schema object inputs/outputs, `effect` (`pure`, `read`, `write`), and a runtime (`python`, `http`, `mcp`). Python tools use a `module:function` entrypoint with source under `src/`; connected tools name a connection and one declared operation. A top-level module in `src/` can read a packaged static file with `Path(__file__).resolve().parent.parent / "resources" / "name.json"`; a nested module must ascend one additional parent per source subdirectory. The worker extracts only validated `src/` and `resources/` files into its private temporary package directory and makes resource files read-only. A Python tool that needs a connection lists its template IDs in `required_connections` and calls `await context.connections.invoke(slot=..., operation=..., arguments=...)`. The isolated worker forwards that request through a bounded private channel to the Tool Gateway, which performs admission and any network call. The worker receives no credentials or network access. A connection template declares its kind and operation shape; actual endpoint and credential values stay in OneFoundry's connection configuration outside the package. Skills contain bounded Markdown and explicit tool references. Skill text is procedure, never a permission grant.

```python
from onefoundry_sdk import ExtensionManifest, ToolContext, build_package, inspect_package, tool


@tool("summarize")
def summarize(context: ToolContext, text: str) -> dict[str, str]:
    return {"summary": text[:100]}


manifest = ExtensionManifest(id="sample", version="0.1.0", name="Sample")
archive = build_package(manifest)
inspection = inspect_package(archive)
assert inspection.package_sha256
```

The ZIP builder sorts members, fixes timestamps, emits canonical JSON, and applies byte, member, path, link, UTF-8, and secret-like-value checks. Inspection exposes `manifest`, `manifest_json`, `package_sha256`, `file_sha256`, and `total_expanded_bytes`. Those hashes identify exact bytes; they do not authorize execution.

For portable procedures, `onefoundry-sdk export DIRECTORY --format codex-portable|codex-legacy|claude --output FOLDER` writes plugin metadata and `SKILL.md` files. `onefoundry-sdk import FOLDER --format ... --output DIRECTORY` creates an inert draft. Bounded UTF-8 `resources/` Markdown, text, and JSON files and original `LICENSE`/`NOTICE` text round-trip as inert bytes; executable and sensitive files are rejected. Unknown authorship is omitted from exported metadata. Recognized static remote MCP declarations retain HTTPS endpoint hints, operation IDs, and resource references as inactive connection templates; they create no tool grants or active connections. Export includes these remote templates for host review. Local MCP commands, hooks, commands, agents, and scripts are rejected or reported inactive and are never executed during import. Format support is an offline projection; destination compatibility still needs validation in that host.

Developers can run `PYTHONPATH=src pytest -q tests`, `ruff check src tests`, `ruff format --check src tests`, `mypy src`, and `uv build --offline` from this directory. This package depends only on Pydantic and `jsonschema`; `jsonschema` 4.26.0 declares MIT in its installed metadata. The JSON Schema validator rejects external and dynamic references.
