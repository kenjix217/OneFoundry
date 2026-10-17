# Personal read-only mail plugin

The source lives in `src/onefoundry/mail_plugin_*.py`; plugin files live in
`plugins/zoho-mail-read/`. It runs separately from the main app:
`uv run --locked python -m onefoundry.mail_plugin_service`.
It has only `mail_search` and `mail_read`, requires OAuth on every MCP request,
and binds loopback. The command creates no persistent service registration,
credentials, grants, or remote route. Missing configuration makes startup fail closed.

The service reuses the existing verified mailbox account and permission-locked
credential store; it does not require enabling polling or re-entering a saved
password. The allowed project/account, exact username/server/port, folders, and
actor are explicit in [the environment example](mail-plugin.env.example).
Explicit settings override environment values; no dotenv file loads implicitly.
JSON allowlists must parse correctly even when explicit settings are provided.
The existing `Settings` owns DB configuration, `MODEL_SECRET_ROOT`,
`MAIL_TIMEOUT_SECONDS`, and `MAIL_MAX_MESSAGE_BYTES`.

Search accepts ASCII text/from/subject filters, inclusive `since` and exclusive
`before` YYYY-MM-DD dates, `before_uid`, and a limit of 1–20 (further capped by
`MAX_RESULTS`). It searches the newest bounded UID window and fetches only capped
headers. Pass `next_before_uid` to continue; one window is not a full-mailbox
search. Reads return capped plain text, exclude attachments, and flag truncation.
Signed IDs include UIDVALIDITY and bind project/account/server/folder/UID. Changed
account bindings or mailbox epochs fail. All email fields are untrusted data.
TLS verification, EXAMINE, BODY.PEEK, byte bounds, one concurrent call, rate bounds,
and a total socket deadline apply. Calls have no retries; cancellation closes the
worker's session at its next bounded socket operation. `busy`, `rate_limited`,
`invalid_input`, scope/binding, credential, audit, TLS, timeout, stale-ID, and IMAP
errors are distinct fixed codes. Tool Gateway events record identifiers, action,
phase, elapsed time, and outcome; no query, message ID, body, subject, or token.
Audit failure prevents reading or releasing a result. HTTP/SDK/SQL logging is off
for this separate process; main OneFoundry logging is unaffected.

## Connection and secure handoff

The operator selected [Zoho-hosted MCP](mail-plugin-connection.md), using Zoho OAuth
without Auth0. That connection is separate from this local read-only companion.
The same runbook retains Secure MCP Tunnel with Auth0 as an unselected local
alternative, including account checks and the [tunnel template](mail-plugin-tunnel.env.example).
The existing Zoho credential stays in OneFoundry's store. Local source, a healthy
process, and an uploaded source ZIP do not prove a cloud Dotty connection.

`PUBLIC_URL` is the resource URL advertised by the local server. With the tunnel,
it must exactly match the HTTP loopback listener; `OAUTH_AUDIENCE` must be the
actual external HTTPS resource from tunnel metadata. Direct HTTPS mode retains
`OAUTH_AUDIENCE`'s default of `PUBLIC_URL`. `OAUTH_AUXILIARY_AUDIENCES` defaults to
`[]`; only Auth0's exact HTTPS UserInfo URL may additionally be configured. JWTs
must still contain the required MCP audience, without duplicate or other audiences.
RS256 issuer, approved subject/client, scope, and bounded `iat`/`exp` remain required.
Key rotation requires replacing the approved public verification key and restarting;
revoked tokens can remain valid until expiry. Stop the tunnel for immediate cutoff.

The package builder's `--verified-mcp-url` option is for a verified ordinary remote
HTTPS connection. A Tunnel connection is selected in ChatGPT's supported UI; do
not fabricate it as a portable `mcp.json` URL or upload the source ZIP as connected.

Live acceptance: discover exactly two tools from Dotty, search one approved
folder, read one returned ID, verify the message remains unread and the timeline
has content-free receipts, then reject an unapproved folder and stale ID.
No desktop/mobile UI was added; browser acceptance remains the host's OAuth/plugin
flow and is pending until the approved tunnel exists. No outreach is authorized.
