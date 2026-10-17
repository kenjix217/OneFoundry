# OneFoundry Company Memory

Private portable Agent Plugins 1.0 source for Codex and ChatGPT/Dot. A source-only
archive has no live endpoint, credentials, or installed connection. The approved
OneFoundry memory service remains required. Local Codex supports a scoped API key;
Auth0 is optional and is not a OneFoundry dependency.

The companion service reuses OneFoundry's existing Postgres/DreamCycle truth.
Search/read return current reviewed facts. Capture, when explicitly granted,
archives only supplied visible company excerpts and creates a pending candidate.
The local operator reviews changes. No arbitrary OneFoundry controls are exposed.

For a verified direct HTTPS endpoint, package with
`python -m scripts.package_memory_plugin --output <new-archive> --verified-mcp-url <approved-endpoint>`.
This adds portable `mcp.json` without authentication material. Secure MCP Tunnel
uses the host's native Tunnel connection flow; do not invent a direct server URL
or package a placeholder as a working connection. Each host must complete its
supported authentication flow and harmless search/read acceptance before claiming live readiness.

For local Codex, `--local-client-config <owned-0600-client-JSON-path>` packages the
stdio bridge and its real configuration path. The file contents are never exported.
The bridge reads a dedicated read-only API key from that private file, connects only
to the configured loopback/HTTPS MCP endpoint, and refuses redirects and ambient
proxies. The server stores only the key digest, checks expiry and project/company
grants, and reloads digests on each request for immediate revocation. No OAuth issuer
is required in API-key mode.

A separately approved capture actor/key may expose `memory_capture_preview` and
`memory_capture`. Preview the exact selected excerpt, fact, project and scope,
show the source-retention notice and wait for human confirmation before submission.
The archive persists even if review rejects the pending candidate. Whole-chat or
automatic capture stays off. Old key records omit scopes and remain read-only;
the capture key must explicitly declare both `memory.read` and `memory.capture`.

For private cloud Work/Dot, an approved Secure MCP Tunnel runs the same bridge on
the operator's machine, using a separate read-only key/actor. The native Tunnel
app uses No authentication at the MCP layer; private organization/workspace/app
access and the locally authenticated bridge enforce the single-operator boundary.
Auth0 is not required for this path. Verify actual native calls and Gateway receipts;
the host must remain awake with the supervised services running.

Runtime and connection details are in `docs/company-memory.md` in OneFoundry.
