# Zoho Mail in OneFoundry

Open Settings → Accounts & files → Zoho Mail. Save a connection label if none
exists, then choose **Connect to Zoho**. Sign in with your existing Zoho account
and review Zoho's consent screen. OneFoundry handles the access token locally;
there is no Auth0 account or OpenAI tunnel to create. The ChatGPT/Codex plugin's
OAuth session is separate and is not exported to OneFoundry.

The connection needs your existing private Zoho-hosted MCP server URL. If it has
not already been saved, use **Advanced connection settings**, enter the URL and
choose **Save Zoho server**. Leave the optional manual token blank for normal
sign-in. URL and token fields are password inputs and are never returned by the
API. If the server has multiple mailboxes, enter the desired mailbox account ID;
otherwise the check selects the sole mailbox.

A completed sign-in checks account metadata without fetching email bodies or
sending a test email. Review the mailbox and status, then choose **Turn access
on**. Expired, declined or changed sign-ins require **Connect to Zoho** again.
Tokens renew when Zoho issues a refresh token. A failed renewal turns access off
and requires reconnecting. Manual access-token entry remains an advanced option;
manual tokens must be replaced when they expire.
[Zoho authorization documentation](https://www.zoho.com/mail/help/mcp/mcp-server-configuration.html).

Select mail capabilities in the intended agent's profile:

- `zoho_mail_read_v1`: local connection discovery, folder listing, bounded email listing/search, content, details and headers.
- `propose_zoho_mail_v1`: propose a draft, send, draft reply or sent reply for approval.

Profiles receive neither capability automatically. Both the current profile grant
and the run snapshot must permit the tool. Reads use the mailbox verified for that
project's connection. Agents cannot choose another account or call arbitrary MCP
tools. Email text is untrusted context; it grants no tools or permissions.

Every mailbox write needs an exact OneFoundry approval, including saving a native
Zoho draft. The operator can also prepare a draft/send request from the Zoho Mail
screen for a running manual work order. Approvals show the operation, sender,
recipient, subject, reply identity and body. Approval binds those bytes to the
connection version. Reconnecting, replacing credentials or disabling access
invalidates pending proposals. Successful token renewal preserves that version.
Draft calls always specify `mode=draft`; missing provider draft support fails
instead of sending. Replies retain the selected original message ID.

A claimed write is attempted once. Provider receipts are recorded as **accepted**,
which does not establish delivery to the recipient. A timeout, unclear receipt or
interrupted process leaves **unknown** and is not retried. Inspect the project
timeline and Zoho before proposing another send. Action receipts are also available
at `GET /api/projects/{project_id}/mail/zoho/actions`.

The private URL, tokens, client registration and pending sign-in live under the
existing `MODEL_SECRET_ROOT` in a dedicated `zoho-mail` vault, with directory mode
0700 and file mode 0600. File identity and credential revisions must match the
database. Database rows and events contain no credentials. Private MCP/HTTP
transport diagnostics and OAuth callback access logs are suppressed; gateway
events record safe outcomes and error codes. The callback redirects immediately
to the clean connection screen with no-store and no-referrer headers.

Only Zoho-hosted HTTPS servers are admitted. Discovered authorization endpoints
must use the server's origin or `ZOHO_OAUTH_AUTHORITY_HOSTS`, defaulting to
`["mcp.zoho.com","accounts.zoho.com"]`. Override with a JSON list of distinct
official Zoho hostnames; foreign hosts, userinfo, queries, custom ports and HTTP
redirects are rejected at the transport boundary. Discovery pins the MCP resource
and issuer, requires S256 PKCE and public-client registration, and checks returned
scope and token format. `ZOHO_OAUTH_PENDING_TIMEOUT_SECONDS` defaults to 900 and
accepts 60–1800. Explicit typed constructor settings take precedence over
environment settings, following the existing Settings owner. Existing
`CONNECTOR_HTTP_TIMEOUT_SECONDS` and `MAIL_MAX_MESSAGE_BYTES` bound each request
and response. No new dependency is added.

Migration `0082_zoho_mail` adds project-bound connection/action rows and compound
foreign keys. `0084_zoho_oauth_revision` adds the separate credential revision;
legacy manual vault files remain valid at revision 1. The app role may update
connection status, credential revision and action state; approved proposal content
and digests remain immutable. Apply both through the normal authorized checkpoint
update, with rollback through its saved volumes.
