# profile improvement proposal

Use this procedure when the operator asks for a reviewable improvement to a named **other** profile. The Chief of Staff or a separate reviewer may use supplied run records and, when admitted, `recent_project_work_v1`, `run_evidence_v1`, and `approved_memory_v1`. These reads are bounded and do not expose complete conversations or all profile instructions; request those inputs from the operator when they matter.

1. Record the target profile, project, review window, current instructions and pinned procedures available to the review. Do not treat a recent-work list as complete history or review your own profile.
2. Group repeated failures only when at least two distinct cited run observations support the same pattern. Distinguish a missing rule from an existing rule that was not followed. If the evidence lacks enough detail, report a single-run observation or a gap instead of inventing a pattern.
3. Propose the smallest instruction, procedure, or tool-description change. Show the exact draft text and the evidence each sentence addresses. Check three to five relevant earlier cases, when available, for behavior the proposal must still permit; label an unperformed replay as a thought check.
4. Return the proposal, uncertainty, and a separate operator-owned application step. Save it with `write_text_artifact_v1` or create a follow-up draft with `create_draft_work_order_v1` only when the work order requests that output and the tool is admitted.

This run cannot change a profile, repin a skill, or grant a tool. A proposal is not an applied improvement, and the target profile must not be hot-swapped during the review.
