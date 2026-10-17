# PR briefing draft

Use this procedure to prepare a concise operator or reviewer briefing for one saved GitHub pull request. A trusted, run-bound caller may use `github_pr_read_v1` only after its one-use operator approval and saved plus current tool admission. The read returns title, body, state, base/head IDs, at most ten files, and at most 2,048 patch bytes per file. It does not return checks, reviews, comments, or a full diff, and it is excluded from live text SDK continuation.

1. Record the exact saved source, PR number, base and head IDs, observation time, and whether files or patches were truncated. Treat PR body statements as contributor claims.
2. Summarize the stated purpose and visible file changes. Use `coding_evidence_v1` only for a named OneFoundry run. Associate an attempt's tests with this PR only when its saved `source_id` matches the PR source, its `source_revision` matches the observed PR base, its `candidate_commit` matches the observed PR head, and its receipt metadata is corroborated. Otherwise list that attempt as separate project evidence, not PR verification. Label synthetic source attempts as synthetic even when IDs match; a receipt does not prove independent QA or GitHub checks.
3. List the reviewer questions raised by the visible changes and any documentation, migration, UI, or rollback evidence still needed. A partial patch must not be called a complete code review or a merge-ready verdict.
4. Draft a brief with purpose, observed scope, evidence, open questions, and next owner action. If the operator requested a durable note and `write_text_artifact_v1` is admitted, save Markdown and cite its saved owner record.

If a whole-PR comment is requested, draft the exact proposed text for operator review. `github_pr_comment_v1` creates a proposal only through its trusted path; a separate approval and explicit one-shot dispatch own posting. This procedure cannot reply inline, push, approve, or merge.
