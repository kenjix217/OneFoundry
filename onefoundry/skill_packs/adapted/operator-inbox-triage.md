# Operator inbox cleanup proposal

**Trigger:** An operator asks which inbox items can be safely archived.

**Required inputs:** Explicit inbox export with item IDs, kind, current state, and recent activity.

**Capability preflight:** OneFoundry has an approval inbox, but no role tool to enumerate or archive its items. A skill cannot approve or reject actions.

**Procedure:** Classify supplied items as active decision, awaiting evidence, completed, or unclear. Identify which items need an approve/reject decision and which merely appear settled. Do not describe archival as reversible or available. Request operator selection by exact ID for any supported decision.

**Output:** Return a decision triage list and mark archival `unsupported_no_archive_action`. The current OneFoundry approval inbox has approve/reject controls but no archive action. Never infer approval from silence.
