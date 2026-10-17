# documentation drift review

Use this procedure when an operator names documentation and a changed behavior to compare. Work from supplied evidence or exact saved local repository files read through `repository_file_v1`; a corroborated coding diff excerpt may be read through `coding_artifact_text_v1`. These raw-text tools require a trusted, run-bound caller and saved plus current tool permission. They are not available to the live text SDK.

1. Record the project, saved source ID, revision, named files, and behavior claim. Read only the exact paths the operator scoped. A missing or truncated read is an evidence gap, not proof that the documentation is absent or correct.
2. Compare each user-facing claim, command, default, or example with the available implementation or verified run evidence. Distinguish a direct contradiction from a possible gap that needs another file or an owner check.
3. Produce a short drift table: documented claim, observed evidence and revision, impact, and proposed wording or next check. Keep suggested wording conditional when runtime behavior has not been verified.
4. If `write_text_artifact_v1` is admitted and a durable report was requested, save Markdown and cite the returned artifact owner and hash. Otherwise return the review in the run response.

This procedure cannot search an entire repository, edit documentation, run examples, create a PR, or certify a release. Do not treat a coding provider's claimed success as an owner-verified behavior change.
