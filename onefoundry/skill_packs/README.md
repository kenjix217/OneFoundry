# Reviewed OneFoundry text skill pack

`source_inventory.json` records source IDs, byte counts, and adaptation status. It stores no third-party procedure body, source hash, or license text. Regenerate it with:

```sh
cd onefoundry
.venv/bin/python -m scripts.skill_inventory \
  --paperclip-root /path/to/paperclip \
  --codex-root /path/to/.codex \
  --output skill_packs/source_inventory.json
```

The current local inventory has 53 Paperclip and 182 Codex `SKILL.md` files, plus eight Paperclip team/task templates, four Paperclip workflow references, and one external skill pointer. One hundred sixty-nine original OneFoundry procedures in `adapted/` derive concepts from 168 source skills and all 12 Paperclip workflow sources; 67 `SKILL.md` files outside the requested batch remain inventory-only. Every one of the requested 38 Paperclip, 50 Codex visual/document/template, and 35 Codex engineering/security sources now maps to a procedure. These procedures are OneFoundry-authored adaptations. Source bodies, scripts, credentials, and connector configurations were not copied.

Skill Library titles and descriptions use task names without product or provider prefixes. Existing libraries can be previewed and migrated in place with `python -m scripts.rename_skill_library --project-id PROJECT_UUID --base-url http://127.0.0.1:8765`, then the same command with `--apply`. It keeps skill IDs and immutable history, appends revised active versions, and moves existing pins to matching new versions. Import refuses to create duplicate skills while branded predecessors still exist.

**Current live role-agent tools:** built-in Researcher and GTM, plus custom profiles, can use the OpenAI API-key Agents SDK route for admitted context, arithmetic, and selected draft/artifact actions, subject to saved and current tool permissions. Public-web reads on that route require an admitted built-in Researcher. Other provider transports remain tool-free. Raw repository/artifact text, PR read/comment, and static browser inspection are excluded from this live route; their procedures require a trusted run-bound or operator path. Bound procedures still give guidance rather than tool authority, and their supplied-evidence tasks must not imply live-source access unless an admitted tool actually returned evidence. Local SDK tests do not prove a live account connection.

For tool contracts, see [Skill tool decisions](TOOL_BACKLOG.md). [Paperclip workflow and remaining pack coverage](REMAINING_PACKS.md) reviews the 38 Paperclip, 50 visual/document/template, and 35 engineering/security sources. [Finance and data pack coverage](FINANCE_DATA_PACKS.md) names every source in those three Codex packs. The inventory is the complete source list.

| Paperclip pattern | OneFoundry owner now | Status |
| --- | --- | --- |
| Plan to assigned tasks | Work orders, profile assignment, manual handoff | Adapted planning procedure; operator creates orders |
| Intake and issue triage | Work orders and project context | Adapted triage procedure; no automatic inbox sweep |
| QA acceptance | Run timeline, receipts, artifacts, QA profile | Adapted acceptance procedure; verification is still performed by QA/operator |
| Governed outbound action | Approval inbox and Tool Gateway | Existing gate; a procedure cannot approve or execute |
| Agent creation and training | Profile editor, Skill Library, exact binding | Existing profile controls; skill authoring guidance added |
| Recurring team reviews and heartbeat routines | No installed scheduler for these templates | Priority, design, content, and engineering reviews adapted for operator-started use; no automatic wake or run loop |
| Paperclip API, wiki plugin, browser and email bridge actions | No matching OneFoundry tool contract | Procedures provide scoped preparation and a blocked outcome until an approved OneFoundry owner exists |

The requested Paperclip and Codex sources all have procedure entries. The procedures for missing browser, renderer, scanner, Git, wiki, email, and connected-host capabilities specify a preflight and a preparation or blocked result. They do not make those source workflows operational. An absent source or tool produces a gap, not an invented result. The Codex cache also contains duplicate skill bodies and artifact-template pointers; template procedures prepare native artifact work but cannot clone or render the retained assets. A source record is not an installed skill, executable tool, or permission grant.

The DistrictAI project pins the text procedures as follows:

| Profile | Selected new procedures | Current use |
| --- | --- | --- |
| Chief of Staff | Custom profile design | Manual-run guidance; operator saves a proposed profile |
| GTM | Clear writing, release announcement draft, content calendar draft, KPI design, business decision memo | Live text drafts from supplied facts; outbound action remains approval-bound |
| Researcher | Market sizing, context framing, supplied-data review | Live text reasoning from supplied facts and approved memory |
| QA | Written UX critique | Manual-run snapshot only; QA is not on the live text path |

The procedures added after the initial profile pins are library entries but are not pinned to any profile by these imports. The PR and public-company meeting briefings have reviewed v2 versions; four older procedures with corrected tool descriptions also have new versions. Existing profile pins stay on their selected versions. Live text runs and Task chat automatically match the work order against the project's latest skill metadata, load at most three relevant unpinned procedures within the prompt budget, and keep exact pins ahead of them. Matching is deterministic and may miss different wording; it does not give the model a full catalog search tool. Repository/PR raw reads need a trusted run-bound caller. Public research requires an explicitly admitted built-in Researcher run with configured web access; the current DistrictAI Researcher profile has no versioned web tool IDs in its allowlist. The team reviews are operator-started; no scheduler is installed. Grant needed tool IDs through the profile and Tool Gateway separately when a work order needs them. A procedure cannot grant its own tools.

Preview the reviewed pack, then import it into one chosen project:

```sh
cd onefoundry
.venv/bin/python -m scripts.import_skill_pack --project-id PROJECT_UUID --base-url http://127.0.0.1:8765
.venv/bin/python -m scripts.import_skill_pack --project-id PROJECT_UUID --base-url http://127.0.0.1:8765 --apply
```

The import uses OneFoundry's existing project Skill Library API and checks readback after each write. Repeating it skips only when the **latest** version matches. If it differs, inspect that version and pass `--apply --update` to append a reviewed revision; existing profile bindings do not move. Run one import at a time: the API serializes version numbers but does not deduplicate simultaneous identical imports. A failure can leave earlier skills saved, so inspect the library before rerunning. Optionally pin exact versions in **Team → Edit profile → Pinned procedures**. Pins enter every live text prompt for that profile; automatic matches enter relevant work only. The default total prompt limit is 32,000 UTF-8 bytes. QA can retain a procedure as a manual-run snapshot, but the installed live text path does not run QA. External Codex/Claude coding attempts currently snapshot pinned skill metadata but do not pass procedure bodies into their runner request.
