# Zoho Mail cloud connection

## Selected route: Zoho-hosted MCP

The operator selected the existing Zoho-hosted MCP server and declined Auth0.
Use ChatGPT Plugins → Add → Create custom MCP server, the private server URL,
OAuth, and Zoho's discovered Dynamic Client Registration. The operator completes
Zoho's native consent page. No Auth0 account or running OpenAI tunnel is needed.
Keep the private URL in the protected credential store; it contains a server key
and must not appear in source, plugin archives, screenshots, or diagnostics.

The server now exposes only `getMailAccounts`, `getAccountDetails`, `getAllFolders`,
`listEmails`, `SearchEmails`, `getMessageContent`, `getMessageDetails`,
`getMessageHeader`, `sendEmail`, and `sendReplyEmail`. Set this plugin's permission
mode to **Allow read actions**: reads may proceed, and every write requires approval.
Verify the saved permission and actual tools after OAuth; creation is not acceptance.
Inspect the live composition schema before claiming mailbox draft support. Do not
send test email or substitute an in-chat draft for a saved Zoho mailbox draft.

[Zoho server configuration](https://www.zoho.com/mail/help/mcp/mcp-server-configuration.html)
describes the provider connection. The existing local read-only package below is a
separate alternative and does not describe the hosted server's tool names.

## Local read-only alternative: Secure MCP Tunnel + Auth0

The earlier local runbook keeps the separate mail MCP process on loopback, uses OpenAI's
official `tunnel-client`, and uses a dedicated Auth0 Free tenant for OAuth. This path
is not selected for the current setup; do not create an Auth0 account for it.
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
supports private OAuth MCP connections without inbound ports or a public app listener.
The issuer must still be public; Auth0 supplies that. This is a private developer-mode
connection, not a public plugin submission. It does not use the prior Cloudflare key.

The parent already verified the saved Zoho inbox through its actual server with
polling disabled. Reuse that account and credential; do not ask for another password
or repeat account setup. Synthetic tests do not replace the final Dotty read below.

## Approval bundle and cost

Before creating credentials, grants, profiles, or a live route, obtain approval for:

- One dedicated Auth0 Free tenant, one operator login, one OAuth client/API, and
  the operator's `mail.read` consent. Login identity goes to Auth0; the Zoho password
  stays local and is never supplied to Auth0.
- One OpenAI tunnel associated only with the intended Platform organization and
  ChatGPT workspace; a new runtime key limited to Tunnels Read + Use; and outbound
  HTTPS to OpenAI and the selected Auth0 origin. No inbound firewall/DNS changes.
- A protected local message-ID signing key/config, the issuer's public verification
  key, and launching the separate mail process and tunnel client with same-host
  loopback HTTP for MCP discovery/forwarding. Keep raw HTTP/payload logging off.
- A spend ceiling of $0 in additional paid subscriptions. Stop if the chosen account
  requires an upgrade or fee; do not buy or silently choose public exposure.

[Auth0 Free](https://auth0.com/pricing) is listed at $0/month, includes MCP authentication,
and needs no credit card. Existing ChatGPT plan costs still apply. OpenAI's tunnel
guide does not publish a separate tunnel price: confirm actual access and charges
in the selected account before creation. No domain, VPS, or paid Cloudflare plan is
part of this path. The installed OneFoundry app does not need a restart or migration.

## Setup order after approval

1. **Check access first.** Open [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels)
   in the user's personal organization and confirm Tunnels Read + Manage for setup,
   Read + Use for runtime and the app creator. Confirm the intended ChatGPT workspace
   can be associated. Enable developer mode in ChatGPT Settings → Security and login;
   workspace policy may require an admin. If access/linking is unavailable, stop and
   report that gate before creating Auth0 resources or keys.
2. **Create the private tunnel.** Associate only that organization/workspace and copy
   its actual ID and external MCP resource URL from the host's tunnel metadata. The
   standard shape is `https://api.openai.com/v1/mcp/<tunnel_id>`; copied metadata is
   authoritative. Create a new restricted [runtime API key](https://platform.openai.com/settings/organization/api-keys)
   with Tunnels Read + Use. The user saves it directly to an approved owned 0600 file
   in a 0700 directory; no chat, argv secret, screenshots, or diagnostic capture.
3. **Configure Auth0.** In a dedicated Free tenant, create a Regular Web Application
   using [manual client registration](https://auth0.com/ai/docs/mcp/guides/registering-your-mcp-client-application/manual-client-registration).
   Use Authorization Code + PKCE S256 and `client_secret_post`; do not enable a
   machine-to-machine mail grant. Create an API whose Identifier is the exact external
   MCP resource URL, use RS256, add permission `mail.read`, and allow this client's
   user-delegated access. Set both access-token lifetimes to 900 seconds and its
   [JWT profile to RFC 9068](https://auth0.com/docs/get-started/apis/configure-access-token-profile)
   so access tokens carry `client_id`. In tenant Settings → Advanced enable
   [Resource Parameter Compatibility and Include Issuer in Authorization Responses](https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile).
   Create/choose the one operator login through Auth0's secure UI and record its exact
   `user_id` as the allowed `sub`. No mailbox password is needed here.
4. **Prepare protected runtime configuration.** Copy the ready mailbox's project/account,
   verified username/server/port and credential-store path from OneFoundry. Pin only
   `INBOX`. Use the same approved service identity and existing DB/credential mounts;
   do not give the companion an admin DB credential. Fill [mail settings](mail-plugin.env.example)
   with the exact Auth0 issuer (including its trailing slash), client ID and subject.
   Set `PUBLIC_URL` to `http://127.0.0.1:<approved-port>/mcp`, matching `LISTEN_PORT`;
   set `OAUTH_AUDIENCE` to the external tunnel resource. Set
   `OAUTH_AUXILIARY_AUDIENCES` to `["https://<actual-tenant-domain>/userinfo"]` only
   if that exact URL is Auth0's additional OIDC audience. The required MCP audience
   must still be present ([Auth0 token profiles](https://auth0.com/docs/secure/tokens/access-tokens/access-token-profiles)).
   Retrieve the current public RSA verification key from Auth0's signing-key/JWKS UI,
   convert its public certificate to public-key PEM if needed, and save it as an owned
   regular file not writable by other users. Never store an issuer private key here.
   Generate a random signing key of at least 32 bytes, base64url encoded, directly
   through `ModelSecretStore` at `MODEL_SECRET_ROOT/mail-plugin/<ID_KEY_ID>` with
   directory 0700/file 0600. Do not display it. Explicit settings override environment;
   no dotenv auto-load. Start the separately reviewed source runtime with
   `uv run --locked python -m onefoundry.mail_plugin_service` in that approved environment.
5. **Start the official tunnel client.** Download the supported host binary from
   Platform Tunnels or [the official latest release](https://github.com/openai/tunnel-client/releases/latest).
   Inspect `help quickstart` and `profiles samples show sample_mcp_with_dcr`, then use
   the commands below with actual nonsecret values. The sample supports an HTTP OAuth
   server with a pre-registered client; DCR is not required for this setup. Apply the
   [tunnel settings](mail-plugin-tunnel.env.example) only to this client, including the
   exact trusted Auth0 origin and explicit same-host HTTP allowance. Confirm the generated
   profile has only this main MCP binding and no extra REST forwarding targets. Keep
   it in the foreground during initial acceptance; persistent supervision needs approval.
6. **Create/connect the private app.** At [ChatGPT Plugins](https://chatgpt.com/plugins),
   choose plus → developer-mode app → Connection: **Tunnel** → the actual tunnel →
   Authentication: **OAuth**. Enter the Auth0 client ID/secret only into the native
   secure OAuth setup fields. Copy the exact callback URI displayed by the management
   page into Auth0's Allowed Callback URLs, without a wildcard. With RFC 9207 it may be
   `https://chatgpt.com/connector_platform_oauth_redirect`; otherwise it may be a
   per-connector URI. The actual UI is authoritative ([ChatGPT OAuth](https://developers.openai.com/plugins/build/auth)).
   Finish sign-in/MFA/consent in Auth0's UI. Allow only the intended user's `mail.read`
   grant; authorize rotating refresh access only if approved for continued sessions.
   A created app is not yet proof that Dotty has the tools: select/enable it in the
   intended Dotty context and perform the acceptance below.

```text
tunnel-client init --sample sample_mcp_with_dcr --profile zoho-mail-read \
  --tunnel-id <actual-id> --mcp-server-url http://127.0.0.1:<approved-port>/mcp
tunnel-client doctor --profile zoho-mail-read --explain \
  --control-plane.api-key=file:<absolute-protected-runtime-key-path>
tunnel-client run --profile zoho-mail-read \
  --control-plane.api-key=file:<absolute-protected-runtime-key-path>
```

These are future approved setup instructions, not commands already run. The
[tunnel configuration reference](https://github.com/openai/tunnel-client/blob/main/docs/configuration.md)
defines file-backed keys and precedence: flags > environment > YAML/profile > defaults.
Do not use its optional Codex-install commands or change Codex authentication/settings.

## Parent-owned acceptance and remaining inputs

Confirm local protected-resource metadata and the external rewritten resource;
`doctor --explain`/readiness must pass or identify only the expected OAuth challenge.
Then verify authenticated initialize/tools/list from cloud Dotty shows exactly
`mail_search` and `mail_read`. Search the approved inbox, read one returned signed ID,
and compare unread state before/after. Check content-free Tool Gateway receipts and
reject an unapproved folder and stale ID. Do not capture message text or credentials
in logs/screenshots. Verify the connection/consent UI at desktop and mobile viewports.

Missing nonsecret inputs: actual organization/workspace entitlement and fee check;
tunnel ID/external resource; Auth0 tenant issuer/public key/client ID/operator subject;
the UI's exact callback; and approved local port/key/config paths. The existing mailbox
IDs and credential are operator-local readback, not new user password inputs. Live auth,
tunnel readiness, cloud tool connection and browser acceptance remain unexecuted.
Stopping the tunnel cuts access; OAuth/public-key and message-ID key rotation require
controlled restart. Short-lived issuer tokens may otherwise remain valid until expiry.

`zoho-mail-read-source.zip` is source-only. Use the Tunnel UI connection rather than
inventing a remote `mcp.json` URL or treating a ZIP upload as live access. Attach/update
the read-only procedure on the actual private plugin only after its connection is
verified through the host's supported flow; do not create a second placeholder plugin.
