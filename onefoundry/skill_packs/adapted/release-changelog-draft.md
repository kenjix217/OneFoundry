# release changelog draft

Use this procedure to draft user-facing release notes from a supplied, operator-verified candidate range. The operator must provide the release identifier, prior release notes, dated change or PR evidence, and known migration or compatibility facts.

1. Record the exact candidate revision or range and the source of each input. If the range or release identifier is unverified, label the result a working draft and request owner verification.
2. Compare the prior notes with the candidate changes. Describe only the new behavior in this candidate; group related changes and omit internal work with no user effect.
3. Separate breaking changes, highlights, improvements, fixes, and upgrade actions. Flag possible breaking changes whose migration impact is not yet established. Attribute a PR or contributor only when the supplied evidence establishes the link.
4. For each entry, identify the supporting source ID and whether the behavior was tested or only reported. List missing runtime, packaging, or rollback evidence for operator review.
5. Return the proposed notes and a short unresolved-facts list. Save Markdown with `write_text_artifact_v1` only when admitted and requested; report its saved ID and hash.

This procedure does not derive a version, inspect Git tags, modify release files, publish packages, or declare a release shipped. Release announcement copy is a separate procedure.
