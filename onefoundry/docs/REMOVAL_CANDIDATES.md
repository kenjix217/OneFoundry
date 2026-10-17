# Removal candidates

Dead or redundant code noticed during implementation, kept here for a later cleanup pass. Nothing listed is removed yet.

| Found in | Item | Why it can go | Safe removal step |
| --- | --- | --- | --- |
| Coordination Slice 1 | `coordination_intents.step_key` | Duplicates `action_kind`; kept only because the 0039 unique constraint `onefoundry_intent_once` uses it. | New migration: rebuild that constraint on `action_kind`, drop `step_key` and its values. |
| Coordination Slice 1 | Constraint, foreign-key, and index names prefixed `onefoundry_intent*` on `coordination_intents` | Leftover names from the renamed 0039 table. | Rename in a migration; no behavior change. |
| Coordination Slice 1 | Settings `ONEFOUNDRY_COORDINATOR_POLL_SECONDS`, `_LEASE_SECONDS`, `_MAX_ATTEMPTS`, `_ACTOR_ID` | They now govern the general coordinator, not only business mode. | Add `COORDINATION_*` names, accept the old names for one release, then remove them. |
| Coordination Slice 1 | `src/onefoundry/onefoundry_intent.py` | A small module for one built-in action; it belongs with `coordination_store.py`. | Move `enqueue_draft_intent` and `latest_intent` into the coordination store and update three imports. |
| Coordination Slice 1 | Makefile target `check-onefoundry-event` | Named after the removed Compose profile `onefoundry-event`; still works. | Rename with the settings above. |
| Pre-existing on `main` | `tests/test_skill_selection_unit.py`: `test_presentation_aliases_route_real_pack_metadata`, `test_document_and_spreadsheet_aliases_route_real_pack_metadata` | They expect skill names with the old `OneFoundry ` prefix, which the skill pack no longer uses; both fail on unmodified `main`. | Update the expected names or delete the two stale assertions. |
| Slices 3–8 | `ModelSecretStore` | Now also stores mail passwords, connector tokens, and GitHub tokens; the name is misleading. | Rename to a neutral secret store and keep the on-disk layout. |
| Slices 7–8 | `onefoundry.inference.http.send_http` | Used by connectors and drive adapters, not only inference. | Move to a shared `onefoundry.http` module and re-export from the old path for one release. |
| Slice 4/7 | IMAP poll path in `mail_store.py` | Builds the signal field dictionary inline; the API mailbox path uses `_fields`. | Use `_fields` for both paths. |
| Pre-existing | Credential resolution in `live_text_store.py` and `task_conversation_store.py` | The same account and secret checks are written twice. | Extract one binding helper used by both. |
| Slices 3–9 | Role tool modules (`role_report_signal.py`, `coordination_profiles.py`, `document_writes.py`) | Each repeats the same call-ID, prior-attempt, and receipt boilerplate as older write tools. | Extract a small write-tool runner in `role_tool_admission.py`. |
| Audit | Startup orphan cleanup for `mail` and `connectors` secrets | Deleting an account removes its secret, but a crash between saving the secret and the database row leaves an orphan file that nothing removes. | Reconcile both subdirectories against their tables at startup, as model and GitHub secrets are. |

Fixed during Slice 1 rather than listed: `db_roles.py` omitted the old intents table from its revoke list (now covered under the new name), and the frontend declared `DeliverableKind` twice (now one type). A reference scan of the modules touched in Slice 1 found no unused functions.
