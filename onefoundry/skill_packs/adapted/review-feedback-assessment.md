# review feedback assessment

Trigger: The operator wants to evaluate code-review feedback before assigning changes.

Inputs: Exact comment, patch revision, affected contract, relevant source excerpts, and any test or runtime evidence.

Capability preflight: Confirm the comment and revision are supplied or available through an admitted trusted read. `github_pr_read_v1` needs a saved source and one-use approval; raw repository and artifact reads are unavailable to live text SDK continuation.

1. Restate the reviewer's claimed failure in concrete behavior terms.
2. Trace the claim through visible callers and tests; identify whether it is introduced by the patch or pre-existing.
3. Classify feedback as supported, contradicted, or unresolved with source locations and missing checks.
4. Recommend a bounded change or evidence-backed response, leaving implementation and PR commenting to separately authorized paths.

Output: One verdict per comment and next action. Do not accept or dismiss feedback by tone, reviewer identity, or an incomplete code excerpt.
