# Event-driven coordination: design plan

Status: Slices 1–9 implemented on 2026-10-01 (see `PROGRESS.md`); Slice 0 was implemented separately. Live acceptance with the operator's own mailbox, Microsoft and Google app registrations, and model accounts is still pending. Documents priority was not decided, so Microsoft and Google were built together.

## Desired outcome

The operator should not have to keep agents busy through chat. Things that happen, such as an email arriving, a payment landing, a run finishing, an alert firing, or an agent discovering something, should start the right work automatically. That work stays inside rules the operator owns, runs under budgets, and still stops for approval before anything leaves the machine.

Seven outcomes define success. Every slice below traces back to at least one of them.

| # | Outcome | How this design delivers it |
| --- | --- | --- |
| O1 | Work starts from events, not from the operator typing | Watchers turn outside activity into signals; the coordinator turns signals into work orders |
| O2 | The coordinator can create work orders itself | Rules and Chief of Staff triage produce saved intents; the coordinator fulfils them |
| O3 | Monitoring agents watch channels and report discoveries | Watchers observe channels without spending tokens; agents report findings through one tool; both become signals |
| O4 | No agent chatter and no runaway token spend | Agents never message each other; every trigger is bounded by rule budgets, chain depth, de-duplication, and a pause switch |
| O5 | The operator owns the workflow | Rules are operator-authored and versioned; autonomy is set per rule; outbound actions always need approval |
| O6 | The Chief of Staff can create the agent a task needs | The Chief proposes a new profile; the operator approves it in the inbox |
| O7 | Agents work with the operator's real mail and documents | Provider-neutral mail and document connectors (IMAP, Gmail, Microsoft 365, Google Workspace, OneDrive/SharePoint) feed signals and context; every write waits for approval |

## What exists today (verified in code)

- `onefoundry_intents` (migration 0039) is a durable, leased, idempotent continuation queue, but its `step_key` is limited to `draft_outreach` and it is tied to organization workflows.
- `onefoundry_coordinator.py` is a separate Compose service with database access only and no provider credentials. It claims due intents using `FOR UPDATE SKIP LOCKED`, blocks an intent after too many attempts, and creates one ready Sales work order. It never starts a run. It runs only in the `event`/`inbox` business mode, which is off by default.
- `remote_channel_runtime.py` runs inside the app process. It receives Discord and WeChat direct messages and answers them through the Chief of Staff's task chat. This is the only live path where outside input causes a model call without the operator.
- `create_draft_work_order_v1` lets an active role run save a `draft` work order. `request_qa_review_v1` records a request. Neither starts work.
- `onefoundry_budget.py` provides atomic workflow admission, reservations, and `budget_exceeded`/`usage_unknown` blocking.
- The `events` table is append-only and enforced by a trigger. Only gateway-owned code writes to it.

Nothing yet watches email, webhooks, schedules, or alerts, and no general mapping from events to rules to work exists. This plan generalizes the pieces above rather than replacing them.

## Core model

```
 Watcher ──┐
 Agent finding (report_signal_v1) ──┼──► Signal ──► Rule match? ──yes──► Intent ──► Coordinator ──► Work order ──► Run
 Internal event (run done, approval)┘                 │                                         (draft / ready / auto-start)
                                                      no
                                                      ▼
                                          Chief of Staff triage (one bounded call)
                                                      ▼
                                      Intent (draft by default) or "ignore" or "ask operator"
```

Five rules hold the design together:

1. **A signal is data, never an instruction.** Signal content is untrusted. It is quoted into a work order as labelled evidence and cannot grant tools, approve anything, change rules, or pick an autonomy level.
2. **Watching costs no tokens.** Watchers are plain connectors. A model is called only when a rule says a signal needs work, or when an unmatched signal goes to triage.
3. **Agents never talk to each other.** An agent's only way to cause more work is to report a finding, and that finding goes through the same rules as any other signal.
4. **The work order stays the unit of work.** Events create and advance work orders; they do not replace them. Timelines, approvals, memory, and evidence remain unchanged.
5. **The coordinator stays deterministic and credential-free.** It matches, queues, and creates. Model calls and run launches happen in the app process through the Tool Gateway, as remote channels already do.

## Data model

All tables carry `project_id` and `actor_id`, following the existing convention. Proposed migration numbers start after the current head (`0041`).

**`coordination_signals`** records one observed occurrence.
- `source` (`internal`, `schedule`, `email`, `webhook`, `channel`, `agent_finding`, `monitor`), `signal_type` (for example `email.received`, `run.finished`, `agent.finding`), `dedupe_key` (unique per project and source), and `observed_at`.
- `fields` holds bounded, normalized JSON that rules may match on (sender domain, subject category, amount, severity). `body_ref` and `body_sha256` point to raw content kept in the private artifact volume; raw text never enters `events`.
- Lineage: `origin_run_id`, `origin_work_order_id`, and `chain_depth`.
- Depth counting: a signal from outside or from the operator starts at depth 0; a work order created from a signal is one deeper; an agent finding inherits its run's depth. Work the operator creates by hand starts a new chain. An inbound email that replies to a thread OneFoundry sent (matched by `In-Reply-To`/`References`) inherits the depth of the work order that sent it, so an auto-responder ping-pong cannot escape the limit.
- `state`: `new → matched | triage | ignored | blocked | done`.

**`coordination_rules`** holds operator-authored playbooks, versioned and immutable per version like skills.
- Match: `source`, `signal_type`, and simple equality or allow-list filters on `fields`. Rules cannot use regular expressions over untrusted free text.
- Action: target profile, title and instruction templates (the signal is inserted as a quoted evidence block), and an `autonomy` of `draft`, `ready`, or `auto_start`.
- Limits: `max_per_day`, `max_model_attempts`, an optional `max_chain_depth` that may be lower than the global setting but never higher, and optional quiet hours.
- `enabled`, plus the version pinned on every intent it creates.

**`coordination_intents`** generalizes `onefoundry_intents`. It keeps the same lease, attempt, and idempotency columns, and adds `signal_id`, `rule_id`, `rule_version` (or `triage_run_id`), `autonomy`, and `target_profile_id`. Slice 1 moves the existing `draft_outreach` continuation into this table so there is one queue, not two.

**Work orders** gain a nullable `origin_signal_id` and `chain_depth`, so every automatically created work order shows what triggered it.

## Components

**Watchers.** Each watcher is a small adapter that polls or receives, normalizes, de-duplicates, and records signals through a gateway method with its own actor ID, for example `watcher:email`.
- Schedule and internal watchers run beside the coordinator and need no network access.
- Watchers that need network access and a credential, such as email, run in the app's channel runtime at first, reusing the protected secret store and the existing lifecycle of heartbeat, backoff, and reported status. A later slice can move them into their own container if stronger isolation is wanted.

**Monitoring roles.** "A sub-agent that monitors a channel" means a watcher plus an assigned role. The watcher does the observing for free. When a signal matches, the rule creates a work order for that role, such as Sales for the sales inbox or Infra for alerts. The role wakes only when there is something to look at.

**Coordinator.** This is the existing service, extended. It claims pending signals, matches enabled rules, applies de-duplication, chain-depth, and budget checks, and writes intents. It then fulfils intents by creating work orders at the rule's autonomy level. For unmatched signals it queues a triage intent. It never calls a model.

**Launcher (in the app).** The launcher picks up `auto_start` and `triage` intents and starts them through the existing Tool Gateway paths (`run_live_text`, or task chat for triage). It uses the same prompt, output, and time limits, and the same handling for unknown outcomes. Unknown outcomes are never retried automatically.

**Chief of Staff triage.** Triage is one bounded structured call, not a loop. The Chief receives the signal's normalized fields, a quoted excerpt, the list of profiles, and the rule catalogue. It returns strict JSON: `{"action": "create_work_order" | "ignore" | "ask_operator", "target_profile_id", "title", "instructions", "reason"}`. Invalid output becomes `ask_operator`. Work orders created by triage are `draft` unless the operator sets a separate triage autonomy ceiling.

**`report_signal_v1` tool.** This is a new role tool. It is off by default and needs both the saved and current profile allowlists, like other tools. An agent reports a finding with a type, a summary, and bounded fields. The result is a signal with `source=agent_finding`, lineage to the reporting run, and `chain_depth + 1`.

## Connectors (O7)

### Mail: connect to the mailbox, not to the mail app

Outlook, Apple Mail, and Thunderbird are only clients. A Zoho mailbox set up in Outlook over IMAP still lives on Zoho's servers, and Microsoft's Graph API cannot see it: Graph reaches only Exchange Online and Outlook.com mailboxes. An "Outlook connector" would therefore miss the operator's Zoho mail. OneFoundry connects to the mail server directly. Outlook keeps working as the operator's client, and both see the same mailbox because IMAP keeps them in sync.

One `MailConnector` contract, with adapters behind it:

| Adapter | Covers | Sign-in | Notes |
| --- | --- | --- | --- |
| **IMAP (first)** | Zoho, Fastmail, iCloud, most hosts, and Gmail or Microsoft 365 as a fallback | App-specific password, or OAuth `XOAUTH2` where the host requires it | Zoho uses a region-specific host (for example `imap.zoho.com`, `imap.zoho.eu`, `imap.zoho.in`) on port 993 with TLS. IMAP access must be enabled in Zoho Mail settings, and an app-specific password is needed when two-factor sign-in is on. Microsoft 365 no longer accepts passwords for IMAP, so it needs OAuth. |
| Gmail API | Gmail and Google Workspace | OAuth, local loopback redirect | Label-based watching; finer scopes than IMAP. |
| Microsoft Graph mail | Exchange Online and Outlook.com | OAuth device code, like the existing Codex sign-in | Delta queries for new mail. |
| Zoho Mail API (optional) | Zoho | OAuth | Only if IMAP proves insufficient. |

How the IMAP adapter is made to work with Zoho, and not just in theory:

- **Probe on connect.** Check TLS, sign-in, `CAPABILITY` (including `IDLE`), the folder list, and `UIDVALIDITY`, then show the result on the Integrations page before the account can be enabled.
- **Stable identity.** A message's `dedupe_key` is its host, folder, `UIDVALIDITY`, and UID, with `Message-ID` as a secondary check. A reconnect or a resync never produces a duplicate signal.
- **Read-only by construction.** The folder is opened with `EXAMINE` rather than `SELECT`, so fetching cannot set the Seen flag or change the mailbox. Sending mail is a separate, approval-bound action.
- **Near real time with a fallback.** Use `IDLE` where the host supports it, and bounded polling otherwise.
- **Tests at three levels.** A local IMAP test server in CI covers normal flows, duplicates, reconnects, and `UIDVALIDITY` changes. Recorded protocol transcripts from Zoho, Gmail, and Microsoft 365 cover their quirks. One live acceptance run against the operator's real Zoho account is recorded in `PROGRESS.md` before the adapter is called working.

### Documents and drives

One `DocumentConnector` contract: list, search, read, watch for changes, and approval-bound write.

| Adapter | Covers | Reading | Watching |
| --- | --- | --- | --- |
| Microsoft Graph files | OneDrive, SharePoint, Word, Excel, PowerPoint | Download the file, or use the Excel workbook API for ranges | Graph delta queries on selected folders |
| Google Drive | Drive, Docs, Sheets, Slides | Export Docs, Sheets, and Slides to DOCX, XLSX, and PPTX (or text, CSV, and PDF); plain download for other files | Drive changes feed on selected folders |

- **Reuse the existing input-file pipeline.** Fetched files are saved to the private input-file volume and extracted by the restricted `input-file-worker`, which already handles PDF, DOCX, XLSX, PPTX, and OCR. Agents receive bounded, labelled text with a source link and hash, the same as uploaded files today.
- **Folder-scoped access.** The operator selects the folders or files an agent may use. Scopes are the narrowest available: Graph `Files.Read` or `Sites.Selected`; Google `drive.file` or `drive.readonly`.
- **Changes become signals.** For example, "new file in /Contracts" or "pipeline sheet edited" are signals of type `document.created` or `document.changed`, handled by the same rules and budgets.
- **Writes wait for approval.** Creating a document, uploading a new version, editing sheet cells, and sharing are outbound actions. The approval shows the exact file, location, and content hash, and a readback confirms the result.

### Accounts, credentials, and sign-in

- **One account per provider.** A single Microsoft or Google sign-in covers both mail and files, with scopes added only when a feature is turned on. Tokens live in the protected secret store beside model credentials, never in PostgreSQL, events, or prompts.
- **Operator-owned app registrations.** The operator registers their own Microsoft Entra app and Google Cloud OAuth client, so no shared OneFoundry client or vendor verification is needed. The Integrations page walks through the setup.
- **Google testing-mode caveat.** A Google OAuth client left in "Testing" status for external users issues refresh tokens that expire after seven days. The setup guide must cover publishing the client, or using an internal Workspace client.
- **Network scope.** Connectors may reach only their provider hosts. Content from mail and documents is untrusted evidence, handled under the same injection rules as signals.

## Guard rails (O4, O5)

| Risk | Guard |
| --- | --- |
| Runaway self-triggering | `chain_depth` is inherited and incremented for each generation of signal to work order to finding. The limit is the validated setting `COORDINATION_MAX_CHAIN_DEPTH` (range 1–30, default 10), shown in the UI; a rule can lower it but not raise it. The coordinator snapshots the effective limit on each intent, so changing the setting never rewrites past decisions. A signal past the limit is blocked with `chain_depth_exceeded` and shown to the operator. |
| Floods (mail storm, alert storm) | `dedupe_key` uniqueness, a per-rule `max_per_day`, a project-wide daily ceiling on automatically created work orders, and watcher rate limits. |
| Token spend | Watchers make no model calls. Triage is one bounded call. Auto-start runs reserve against the existing budget admission, and unknown usage keeps its reservation. |
| Prompt injection from inbound content | Signal text is quoted evidence only. Rules match on normalized fields. Neither triage nor any run can change rules, tools, autonomy, or approvals. Outbound actions still need an exact approval. |
| Losing control | A global `COORDINATION_MODE` setting (`off`, `draft_only`, `auto`, default `off`; only the operator switches to `auto`), a pause switch in the UI that stops new claims immediately, and per-rule `enabled`. |
| Silent failure | Every step writes an event: signal received, rule matched, triage decided, intent queued, claimed, done or blocked, work order created, run auto-started, coordination paused. The payloads contain hashes and IDs only. |

## Chief-created custom agents (O6)

A `propose_profile_v1` tool, available to the Chief of Staff only, saves an exact profile proposal: name, instructions, model, memory scope, requested tools, and autonomy. The proposal goes to the approval inbox and is bound to its SHA-256 digest like other approvals. On approval the Tool Gateway creates the profile under the Chief in the org chart.

- Requested tools must come from an operator-set ceiling list. The Chief can never grant a tool that the operator has not made eligible.
- Autonomy for a proposed profile is capped at `supervised`.
- A proposal can include a first draft work order. It is created only after the profile is approved.

## Build slices

Each slice is independently testable and committed, and records its checks in `PROGRESS.md`.

| Slice | Delivers | Outcomes | Acceptance check |
| --- | --- | --- | --- |
| 0. Job acceptance (prerequisite) | A finished model reply no longer completes a work order. Work orders declare their expected deliverable and acceptance checks; a run that only returns text moves the work order to an approval wait (`needs_review`) unless the declared deliverable is the text reply itself. `done` requires owner evidence (artifact path and hash, saved document, code receipt, confirmed external action) or explicit operator acceptance. Task chat receives a fresh job dossier from canonical records: status, runs, results, handoffs, child work orders, artifacts, approvals, and missing checks. | O2, O5 | The Chief of Staff returns an excellent plan but creates no assignments: the run succeeds, the work order is not `done`, and the next chat reports exactly what is missing. Also covered: missing artifact, failed QA, unavailable tool, restart without duplicate assignments, and a fully evidenced completion. |
| 1. Signals and rules core | The three tables. Internal sources: run finished, approval decided, organization qualified (migrated from `onefoundry_intents`). Rule editor and Signals feed. Autonomy limited to `draft` and `ready`. | O1, O2, O5 | Finish a run and see a signal, a matched rule, and a created draft marked "Triggered by". A second identical signal creates nothing. The pause switch stops new claims. |
| 2. Schedule watcher | Rules with a schedule source (for example "every Monday, research brief"). No network access. | O1 | With a short test schedule, one draft appears per period, and none while paused. |
| 3. Agent findings and Chief triage | `report_signal_v1`. Triage for unmatched signals through the launcher. Chain depth enforced. | O2, O3, O4 | A scripted run reports a finding. Triage produces a draft with lineage. A chain one step past `COORDINATION_MAX_CHAIN_DEPTH` is blocked and visible; an out-of-range setting fails startup validation. Invalid triage output becomes "ask operator". Uses offline scripted-model tests plus one live acceptance run. |
| 4. Mail connector, read-only (IMAP first) | The `MailConnector` contract and IMAP adapter, the connection probe, and an Integrations page entry. Credentials live in the secret store, raw bodies in the private volume. | O1, O3, O7 | A local IMAP test server passes normal, duplicate, reconnect, and `UIDVALIDITY` cases. Fetching leaves the Seen flag unchanged. An injection test shows email text cannot change tools or autonomy. A live run against the operator's Zoho account creates one Sales draft from one test email. |
| 5. Auto-start | Per-rule `auto_start`, the launcher in the app, budget reservation, and daily ceilings. | O1, O4 | An auto-start rule runs within budget. The run after the limit is blocked with `budget_exceeded`. An unknown outcome is not retried. The run timeline shows the triggering signal. |
| 6. Chief-created agents | `propose_profile_v1`, the tool ceiling list, and inbox approval. | O6 | The Chief proposes a profile. Rejecting it creates nothing. Approving it creates exactly the proposed profile under the Chief. Tools outside the ceiling are refused. |
| 7. Microsoft 365 and Google sign-in | One OAuth account per provider (device code for Microsoft, loopback redirect for Google), scope upgrades, and the setup guide. Gmail API and Graph mail adapters behind `MailConnector`. | O7 | Sign in, refresh, and revoke work for both providers. The Google token survives more than seven days. The Gmail and Graph adapters pass the same contract tests as IMAP. |
| 8. Documents and drives, read and watch | `DocumentConnector` for OneDrive/SharePoint and Google Drive, folder selection, Google export to Office formats, reuse of the input-file pipeline, and change signals. | O1, O7 | A selected Drive or OneDrive file appears as extracted context in a work order with a source link and hash. A change in a watched folder creates one signal. Unselected folders are unreadable. |
| 9. Document writes | Approval-bound create, new version, sheet range update, and share, each with readback. | O5, O7 | A rejected write changes nothing. An approved write matches the approved hash on readback. An unknown result is not retried. |

Slice 0 comes first. Every later slice reacts to work finishing; if `done` can mean "the model wrote a plan", automation would chain off false completions, up to the full chain limit. The internal signals in Slice 1 fire only on these verified state changes. The rest of the correction — revisiting a parent job when a child work order, approval, deadline, or failure changes — is not built separately: those changes are internal signals in Slice 1, and Chief triage in Slice 3 receives the Slice 0 job dossier.

Slices 1 to 3 run offline. Slices 4, 7, and 8 add network sources and credentials, so each needs its own security review. Slice 9 is the first to write to the operator's accounts. Slice 5 is where work starts without the operator, so it stays behind `COORDINATION_MODE=auto`.

## Owner decisions (2026-09-30)

1. **Chief triage: approved.** One bounded structured call with no tools and no continuation, as described above.
2. **Mail order: IMAP first**, covering the operator's Zoho mailbox, then the Gmail and Microsoft Graph adapters in Slice 7.
3. **Autonomy: `draft_only` by default.** `COORDINATION_MODE` stays at `off` or `draft_only` until the operator switches it to `auto`; `ready` is not a default.
4. **Chain depth: configurable.** `COORDINATION_MAX_CHAIN_DEPTH` is a validated setting (range 1–30, default 10), not a hard-coded constant; rules may only lower it.
5. **Watcher location: inside the app.** Mail and document watchers run in the app's channel runtime, reusing the secret store. A separate container is not planned.
6. **Existing queue: migrate.** Slice 1 moves the qualified-prospect → Sales continuation into `coordination_intents` as a built-in rule, so there is one queue.

## Open decision

7. **Documents priority.** Which comes first: Microsoft (OneDrive, SharePoint, Office) or Google (Drive, Docs, Sheets, Slides)? And should Slice 8 start read-only, as proposed, with writes held back until Slice 9?

## Implementation audit (2026-10-01)

The built system was checked against this plan after Slice 9.

**Fixed during the audit**
- Concurrent token refresh: API requests and the launcher share the app process and could refresh the same Microsoft or Google token at once, overwriting each other's saved tokens. Refresh is now serialized per account and re-reads the vault after waiting; a test runs four concurrent callers against one refresh.
- Graph mail starting point: `$deltatoken=latest` is documented for directory objects but not clearly for mail folders. The connection check now uses it when Graph honours it and otherwise walks the ID-only delta pages to the end (bounded to 400 pages), discarding items, so no history is imported either way.
- Triage now receives the enabled rule catalogue (names and event types), as the plan describes.

**Built differently from the plan (deliberate)**
- Auto-start spending: launches count against `COORDINATION_MAX_AUTO_STARTS_PER_DAY` instead of the business-workflow budget reservations, because those reservations are tied to organization workflows. Each launch is still one live-text run with the existing prompt and output caps.
- `report_signal_v1`: a finding inherits its run's chain depth, and work created from it is one deeper, following the depth-counting rule above rather than adding one at the finding itself. The effective limit is the same.
- Graph file scope is `Files.Read.All` rather than `Files.Read`, so SharePoint and shared folders can be watched; access is still limited in code to the one selected folder's direct children.
- Watchers write under the launcher actor `coordination-launcher` rather than per-watcher actors such as `watcher:email`.
- The connection checks and setup live on the Automation page, not the Integrations page.
- Triage-created work is always a draft; there is no separate triage autonomy ceiling.

**Open after the original audit (historical; see follow-up below)**
- Email reply lineage (`In-Reply-To`/`References` inheriting the sending work order's depth). OneFoundry does not send real email yet, so the auto-responder loop it guards against cannot occur; it must be added with real outbound email.
- IMAP `IDLE` (polling only), IMAP `XOAUTH2` (Gmail and Microsoft 365 use their APIs instead), and the optional Zoho Mail API.
- Rule `max_model_attempts` and quiet hours.
- Document search, the Excel workbook range API, uploading a new version of an existing file, sheet range edits, and sharing. Writes create new text, Markdown, or CSV files only.
- Microsoft token revocation on removal (local credentials are deleted; Google tokens are revoked).
- A "Triggered by" chip in the Work orders queue and the triggering event in the run timeline; the automatic-creation note currently appears under Task details.
- Live acceptance: the operator's Zoho mailbox, Microsoft and Google sign-in, real drives, recorded provider transcripts, the seven-day Google token check, and any live model call for triage or auto-start.

## Audit follow-up (2026-10-04)

Source now includes reply lineage for the live SMTP path, rule daily admission limits and quiet hours, bounded IMAP IDLE with polling fallback, trigger visibility, direct-folder name search, Excel/Sheets ranges, approved new versions and cell edits, recipient-specific sharing, and a separate confirmed Microsoft sign-out-everywhere action. Migrations 0068–0070 add immutable admission timestamps and restricted action receipts. The README records the tool and settings contracts.

The rule `max_model_attempts` field counts admitted automatic starts per rule per project day across versions; it is not a child-turn token budget. This preserves the existing project daily cap and team limits. Quiet hours delay launches, not signal capture. Microsoft removal stays local; broad session revocation requires its separate permission and confirmation.

File mutations use a preflight revision/cell check and post-write readback, without claiming an atomic lock against external editors. New-version uploads support ordinary stored files; native Google Docs/Slides editing is outside this adapter. General role tools are available to live-text runs; a bridge to continuing team-session tools remains a separate integration gap.

Live Zoho acceptance: the operator's latest “zoho inbox” authenticated over TLS, its normal connection check saved Ready with ingestion disabled, and Zoho accepted a read-only IDLE wait. No notification arrived during that wait. Inbound-message ingestion, triage/automatic-start inference, Microsoft/Google sign-in and disposable-drive mutation, provider transcripts, and the seven-day Google token check remain unexecuted. No real email, sharing invitation, file mutation, or broad sign-out was sent during this follow-up. Optional IMAP XOAUTH2 and the Zoho API remain unbuilt; Gmail and Microsoft mail use their API adapters.
