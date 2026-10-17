# manual engineering review

Use this operator-started procedure to review the next engineering deliverables for one project. The operator starts the review; OneFoundry does not schedule a weekly run, reassign orders, or post status comments for the role.

1. Set the review cutoff and read `project_brief_v1` and `recent_project_work_v1` only when admitted. Ask the operator for named orders outside the bounded recent list. Use `run_evidence_v1` for each material run and `coding_evidence_v1` when a coding attempt matters.
2. Identify work whose next deliverable, owner, or acceptance evidence is unclear. Separate an approval wait, failed run, unknown side effect, and a merely stale display. Treat a test as verified only to the extent its saved receipt and exact source binding support.
3. Give at most three prioritized items with a concrete next deliverable, current owner, blocker, evidence ID, and operator decision needed. Do not infer an engineer is available or that a QA request is a completed QA handoff.
4. If asked, save a Markdown brief with `write_text_artifact_v1` or propose a same-project draft with `create_draft_work_order_v1`, only under saved and current permission. `request_qa_review_v1` can request review of this active review run only; ask the operator to arrange QA for an earlier engineering run. Report the returned owner record and leave readiness, reassignment, QA draft conversion, and launch to the operator.

An operator must start each review. The brief is a snapshot at its cutoff, not a standing board, recurring task, or proof that the top items were acted on.
