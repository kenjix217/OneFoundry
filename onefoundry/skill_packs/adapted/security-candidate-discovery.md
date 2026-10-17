# security candidate discovery brief

Trigger: The operator asks for possible security failures in a named, limited code path.

Inputs: Threat boundary, revision, exact source excerpts or saved file paths, relevant callers and sinks, and any supplied tests.

Capability preflight: Verify admitted trusted access to each exact file with `repository_file_v1` and any saved diff with `coding_artifact_text_v1`, or use clearly labeled operator-supplied text. No general repository search or dedicated security discovery worker exists; raw reads are excluded from live text SDK continuation.

1. Identify attacker-controlled inputs, authority decisions, state changes, and sensitive outputs visible in the evidence.
2. Form discrete candidate failure paths and seek a normal path or downstream control that could falsify each one.
3. For each candidate, record the exact observed source location, missing call sites, preconditions, and confidence.
4. Separate plausible candidates from confirmed findings and name the next source or controlled test needed.

Output: A scoped candidate table or `insufficient_source` result. This is neither a repository scan nor a vulnerability verdict; do not imply coverage beyond inspected material.
