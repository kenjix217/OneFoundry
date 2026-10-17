# contribution review

Use this procedure when the operator supplies a complete, reviewable contribution packet and asks for a decision-oriented code or design review. This is a deeper review than a PR briefing. The packet should name its exact revision and include the full relevant diff, surrounding contracts, contributor intent, and available test evidence.

1. State the revision, scope, and missing parts of the packet. Treat a PR description and provider test claim as claims, not proof. If the packet is incomplete, give a partial review and withhold a merge recommendation.
2. Reconstruct how the affected system starts, runs, fails, and recovers. Trace changed inputs, outputs, persistence, and trust boundaries across the supplied files.
3. List actionable findings first, ordered by impact. For each, cite an exact supplied file and location, the violated contract, and a concrete correction or verification step. Separate product direction, architecture, implementation, rollout, and documentation concerns.
4. Compare external designs only when current, cited source material is supplied or an admitted Researcher has returned it. Do not imply an unperformed repository-wide or security scan.
5. Give a recommendation and the evidence needed before the operator can act. Name tests or runtime checks actually observed and those still missing.

Return a Markdown report. If `write_text_artifact_v1` is admitted and requested, save it and report the saved artifact ID and hash. A trusted run-bound caller may use exact `repository_file_v1` reads; the limited `github_pr_read_v1` response alone is insufficient for a complete contribution review. This procedure cannot approve or merge a change.
