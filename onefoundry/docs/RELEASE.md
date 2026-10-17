# OneFoundry release and software updates

0.19.1 is a patch release completing the existing Grok account connection: automatic device sign-in, reconnect, safe token renewal and streaming text/team runtime. It includes the 0.19.0 conversation changes and adds no migration or new automatic outbound action. Preserve existing private settings: an absent `XAI_OAUTH_CLIENT_ID` uses the published native-client default, while an explicit blank keeps Grok sign-in disabled. Installing the package does not authenticate a Grok account or prove provider access. The supported updater starts the candidate for readiness and checkpoint recovery; a request to leave the stack down must be resolved before activation.

0.19.0 is a minor release for conversation controls, collapsed agent activity, conversation following, per-chat Archive and long-message handling. It adds no database migration, provider permission or automatic outward action. The supported checkpoint update preserves saved conversations, private credentials, project scopes, source mounts and memory assets. Source-control safeguards are development tooling and do not export installed configuration or database records.

0.17.0 is a minor release adding normal Zoho sign-in and protected token renewal. Migration `0084_zoho_oauth_revision` adds an independent credential revision and the corresponding narrow app-role update grant. The supported checkpoint update preserves existing 0.15.0 connection rows and legacy manual credential files. Sign-in and enabling access remain separate operator actions; every mailbox write still requires approval.

0.15.0 adds the scoped Zoho-hosted Mail connector and migration `0082_zoho_mail`. Connections can be saved without credentials and start with access off. Profiles receive no new mail grant automatically; every native mailbox write requires exact approval. OAuth access tokens are entered and replaced manually. Use the supported checkpoint update to preserve existing local configuration, source/memory mounts and all five volumes.

0.14.0 is a minor release for scoped capability readiness and linked self-extension. Migration `0081_repository_code_target` admits the exact maintenance target and extension tool receipts while preserving historical attempts and tool contracts. Existing private configuration, profile grants and source mounts retain their precedence. The five core flags default on; source reads, search, coding and optional renderer/browser operations still require their scoped dependencies. The release packages the separate coding, QA and activation supervisor modules; installing the app does not configure a new provider credential, source scope or host supervisor. Use the supported checkpoint update for rollback.

0.11.1 is a UI patch: one global palette covers Chief and Settings, and a compact Settings page selector replaces the overflowing tab strip. It changes no database schema, agent permissions or workflow execution policy.

0.10.8 adds schema `0077_video_edit_jobs`, qualified native activity/video review and closed-child revisions, the default unlimited SDK model-call policy with explicit finite limits preserved, and the separately launched read-only mail adapter source. A standard update keeps video editing disabled unless its existing approved configuration enables it. The bundle does not grant Docker access to the app, install or launch the standalone renderer, create mail credentials, or activate a new companion connection. Existing private memory asset mounts and companion configuration must survive the update.

0.10.0 follows selective-capture schema 0073 with migration 0074 and the narrow app `task_sessions.execution_mode` update grant. The supported disposable upgrade/checkpoint rollback and local installation passed on 2026-10-06. Existing memory configuration/grants and old drafts are preserved; worker/media/external coding remain disabled. Stop idle companion/trusted-CLI services for the cold checkpoint; the memory tunnel's `Requires` dependency stops it with the companion, so restore that previously active tunnel explicitly after readiness. The optional video path needs an operator-selected window/tab and human microphone narration; live provider execution, actual DistrictAI capture and AI narration are not proved by this release.

OneFoundry has its own `VERSION` and release history. Its versions, tags, artifacts, update state, and installation directories are independent of JintellarCore. The procedure follows JintellarCore's prebuilt package, checksum, persistent data, health check, and rollback pattern. The first supported target is a single Linux host with Docker Compose and the default OneFoundry services. The app remains bound to localhost. No source tree, compiler, or Node runtime is needed on the install host; Python 3.12 and Docker Compose are needed for the operator command.

## Release builder

1. Bump `VERSION`, `[project].version` in `pyproject.toml`, and the root version in `frontend/package.json` together; refresh `uv.lock` and `frontend/package-lock.json` with the repository package managers. Add release notes here and in `CHANGELOG.md`. Use an unreleased version; tag it `onefoundry-v<version>` only after all gates pass.
2. Run `make check` from `onefoundry/`; include migration upgrade, real PostgreSQL, and desktop/mobile browser acceptance for any user-facing change. Review the complete diff and `git diff --check`.
3. Commit the approved release scope. The builder resolves `HEAD` once and exports that exact commit's `onefoundry` tree for its build context, so unrelated dirty working files and concurrent commits cannot enter the artifact. Confirm the commit contains every intended release file before building.
4. From `onefoundry/`, run `python3 -m scripts.release_bundle verify-source`, then `python3 -m scripts.release_bundle build`. This builds version-tagged `runtime`, `runner`, and `input-worker` images, pulls the pinned PostgreSQL and Mailpit images, and writes `dist/onefoundry-<version>.tar.gz` plus `.sha256`.
5. On a clean disposable Linux host or isolated Compose project, use the install command below, exercise the app and data/credential/artifact readback, then test the update and rollback path from the previous release. Record exact image IDs, bundle checksum, schema head, container health, and browser results. Only then publish the tag and archive/checksum through the approved release channel. Record the uploaded checksum and repository commit separately from deployment.

The release bundle contains Docker image bytes, `compose.yaml`, the release image override, the Python operator command, the root MIT `LICENSE`, a manifest, and `VERSION`. It contains no `.env`, database, model credentials, artifacts, or user files. The external `.sha256` must come from a trusted release channel; a checksum delivered only inside the same untrusted archive is insufficient. Signing and remote release discovery are future work. The first updater takes an operator-supplied archive and trusted digest; it does not poll GitHub or download automatically.

## Install and update

Keep the release archive and its trusted SHA-256 digest on the target host. Verify its digest before extracting the operator command. Create a mode-`0600` `.env` from the bundle's `.env.example` with distinct `DB_PASSWORD` and `APP_DB_PASSWORD`; keep it out of the archive and source control. The updater uses that file's values over matching shell variables. Choose an empty install home and a Compose project name unused by any existing OneFoundry stack. These examples assume `ONEFOUNDRY_HOME` points to the chosen install home.

```sh
printf '%s  %s\n' "$TRUSTED_SHA256" /path/to/onefoundry-0.1.0.tar.gz | sha256sum -c -
mkdir -p /private/release
tar -xzf /path/to/onefoundry-0.1.0.tar.gz -C /private/release
cd /private/release/onefoundry
python3 -m scripts.onefoundry_update check --home "$ONEFOUNDRY_HOME" --archive /path/to/onefoundry-0.1.0.tar.gz --sha256 "$TRUSTED_SHA256"
python3 -m scripts.onefoundry_update install --home "$ONEFOUNDRY_HOME" --project onefoundry-prod --env-file /private/onefoundry.env --archive /path/to/onefoundry-0.1.0.tar.gz --sha256 "$TRUSTED_SHA256"
python3 -m scripts.onefoundry_update status --home "$ONEFOUNDRY_HOME"
python3 -m scripts.onefoundry_update stop --home "$ONEFOUNDRY_HOME"
python3 -m scripts.onefoundry_update start --home "$ONEFOUNDRY_HOME"
```

An update loads the verified new images before downtime, stops only the configured Compose project without deleting volumes, snapshots its existing named volumes into `data/backups/`, switches `current`, starts the prebuilt images with `--no-build --pull never`, and waits for `/health/ready`. The migration service runs during Compose startup. If startup/readiness fails, the command stops the candidate, restores the checkpoint, switches back, restarts the old release, and reports the failed update. A recovery failure leaves the exact install/checkpoint state for manual diagnosis; do not assume the old app is healthy.

`--health-timeout-seconds` configures the install/start/update/rollback readiness deadline (1–600 seconds, default 90); `status` uses at most three seconds for a quick probe.

```sh
cd "$ONEFOUNDRY_HOME/current"
python3 -m scripts.onefoundry_update update --home "$ONEFOUNDRY_HOME" --archive /path/to/onefoundry-0.1.1.tar.gz --sha256 "$TRUSTED_SHA256"
python3 -m scripts.onefoundry_update rollback --home "$ONEFOUNDRY_HOME" --restore-data --yes
```

Manual rollback restores the pre-update volume checkpoint, including PostgreSQL and credentials, so **all writes since the update are lost**. Stop inbound work and decide whether those writes need separate export before running it. `rollback` requires both explicit flags and only targets the immediately previous successful update. Keep release directories and backups until rollback is no longer needed. Backups contain protected data and secrets; the updater makes the directory private but does not encrypt it or send it off host. Copy it to an approved protected store before relying on it for disaster recovery. Do not delete named volumes or run `docker compose down -v` in normal operations.

The installed layout is `{home}/releases/<version>`, `{home}/current`, `{home}/data/config/.env`, `{home}/data/updates`, and `{home}/data/backups`. The Compose project name remains fixed in `data/updates/install.json`, preserving volume identity. The installed release does not rebuild source. Optional Compose profiles, custom overrides, remote access, and a multi-host rollout need separate release acceptance before use. Source check and artifact verification do not prove that a currently running Compose stack has been updated.

## 0.7.0 shared company memory

The normal upgrade from 0.6.4 applies migrations 0060–0071: team sessions and attempts, coordination rule limits, connector sign-out, document actions, then company-memory lifecycle and sources. Existing approved facts retain their source lineage; unknown historical runs are not replayed. Conversation capture remains explicit. The separate MCP companion needs its own narrow database login and approved project grants. Local Codex uses a private API key and loopback service through the plugin's stdio bridge. Cloud clients need separately configured OAuth and verified private reachability; updating the app does not activate a cloud connection. Keep capture off for the read-only login.

## Installed operator command (0.1.2 and later)

The verified bundle includes an executable `onefoundry` command. After install or update, run it from `{home}/current/onefoundry`. It infers its install home from the versioned release directory. `onefoundry cli install` creates `~/.local/bin/onefoundry` as a symlink through `{home}/current`, provided that path is free; it never replaces another launcher. Ensure `~/.local/bin` is on `PATH` to run `onefoundry` by name.

To upgrade a 0.1.0 or 0.1.1 install, first verify and extract the 0.1.3 archive as in the install example, then run that archive's `python3 -m scripts.onefoundry_update update --home ... --archive ... --sha256 ...`. The older operator module predates the executable and its expanded bundle inventory.
Version 0.1.3 corrects the `RUNNER_WALL_SECONDS` CLI bound. Upgrade 0.1.2 installations before using `config set` for that setting; if 0.1.2 was set above 60, set it back within 1–60 before restarting.
Version 0.1.4 packages the MIT license and aligns the installed release manifest with the current committed source. It does not change application workflows or the database schema.
Version 0.2.0 adds project Automation, read-only IMAP and API mailboxes, connected accounts, and watched document workflows. It applies additive migrations `0043`–`0051`. Coordination remains `off` until configured; no mailbox credentials or account registrations are included in the archive.
Version 0.2.1 adds outgoing SMTP accounts and exact per-email approvals with additive migration `0052`. Incoming IMAP and outgoing SMTP credentials are separate. Accounts start off until the operator checks the connection and enables sending. Agents need `propose_email_v1` in both their current profile and run snapshot; the tool only proposes a message. Each approval triggers one submission attempt. SMTP acceptance is not recipient delivery, and uncertain outcomes cannot be retried automatically. Batch approval is not yet implemented.
Version 0.3.0 redesigns the operator workspace, groups related settings and workflows, adds local layout customization and focused work creation, and retires superseded UI modules. It keeps the 0.2.1 schema (`0052_outbound_smtp`) and existing installation configuration. Saving a manual result creates a pending memory review; publication still requires approval.
Version 0.5.0 adds a project-level Chief of Staff inbox and routes verified Discord and WeChat personal DMs through it without a selected work order. It applies migration `0058_project_chief_inbox`, clears old channel work-order bindings, and retains confirmed identities and historical receipts. New work and Automation rules still follow existing project controls; Slack and Telegram adapters are not installed.
Version 0.6.0 removes the duplicate web composer from Messaging and adds an optional Discord server-channel route. The server route requires a configured application ID, server ID, text-channel ID, and operator user ID; inbound messages must mention the bot. Migration `0059_discord_guild_channel` is additive. Existing DM bindings retain their state, and no server route is enabled by the update.
Version 0.6.1 makes server setup the visible default for unbound Discord channels and adds field and pairing instructions. Existing personal DM bindings stay in DM mode. No schema or runtime configuration changes are required.
Version 0.6.2 clarifies task-chat send progress, retry and failed-assignment feedback, and the Chief's team-assignment instructions. Existing saved turns and work orders are preserved. No schema or runtime configuration changes are required.
Version 0.6.3 adds a read-only Team tab for saved linked assignments, task chats, and runs, plus a saved-facts work log on each chat reply. Direct agent messages and intermediate text-model progress remain future work. No migration or runtime configuration change is required.
Version 0.6.4 displays bounded Codex account public progress summaries as they arrive in Task chat and preserves them with the turn. It also gives Task chat a separate `TASK_CHAT_TIMEOUT_SECONDS` setting (default 120) after a 30-second provider deadline left a Chief turn unknown. Other providers may still show only the final reply. The update has no database migration; an unknown historical turn remains unknown and is never replayed automatically.

```sh
"$ONEFOUNDRY_HOME/current/onefoundry" cli install
onefoundry version
onefoundry status
onefoundry health
onefoundry doctor
onefoundry config list
onefoundry config explain APP_PORT
onefoundry config get APP_PORT
onefoundry config set APP_PORT 8765
onefoundry restart
onefoundry logs app --tail 100
onefoundry backup --yes
onefoundry check --archive /path/to/onefoundry-0.1.3.tar.gz --sha256 "$TRUSTED_SHA256"
onefoundry update --archive /path/to/onefoundry-NEWER.tar.gz --sha256 "$TRUSTED_SHA256"
```

`config` manages only three non-secret settings already defined by Compose: `APP_PORT` (1–65535), `MAILPIT_UI_PORT` (1–65535), and `RUNNER_WALL_SECONDS` (1–60, matching the app settings contract). `list` reports null for an unset value, meaning the Compose default applies. `set` validates the value, atomically preserves the private `.env`, and requires `restart` to apply it. Database passwords and other secrets remain in the mode-`0600` `.env`; the CLI never prints them. `doctor` is read-only and checks install metadata, configuration, loaded image IDs, volume inventory, required running services, runtime image IDs, and readiness. It exits nonzero for a failed check. `logs` accepts an installed Compose service and at most 1,000 lines.

`backup --yes` briefly stops the installed project, snapshots all five named volumes into the private backup directory, and restarts the same pinned release. A backup is local and unencrypted. The archive-backed `check` and `update` commands still require a trusted SHA-256 digest; remote release discovery and unattended updates are not implemented.

### 0.12.0 conversation navigation and Trash

Minor release: shared conversation navigation across Settings and recoverable conversation deletion. Migration `0080_chief_conversation_trash` adds an execution-held Trash marker; it never deletes history or files. Use the supported update checkpoint for rollback. Settings navigation must preserve the selected conversation and an unsent message on desktop and mobile.
