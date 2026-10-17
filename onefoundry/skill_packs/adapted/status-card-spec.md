# Status card specification

**Trigger:** An operator wants a standing view of a narrow project question.

**Required inputs:** Exact question, project, fields and statuses of interest, update interval, and examples of desired results.

**Capability preflight:** OneFoundry has project context reads but no status-card query compiler, saved card owner, or automatic refresh scheduler. `recent_project_work_v1` is bounded and cannot prove full-search coverage.

**Procedure:** Translate the question into explicit filters and recency limits, flag terms needing IDs or schema mapping, and define a short summary format. Check whether a one-time project status brief suffices.

**Output:** Return a card specification and sample summary from supplied data only; mark creation and refresh blocked.
