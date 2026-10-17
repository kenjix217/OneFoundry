# artifact handoff

Use this procedure when a run produced a deliverable that the operator or QA must inspect. Anchor the handoff to an OneFoundry owner record, not a local path or an agent's claim.

1. Identify the project, work order, run, artifact ID, type, and producing actor. Distinguish a role-authored text artifact from a saved external coding artifact or a merely mentioned workspace file.
2. For admitted `write_text_artifact_v1`, retain the returned artifact ID and SHA-256. Ask the operator to use the artifact readback when byte-level inspection matters. For coding output, cite the saved receipt and `coding_evidence_v1` metadata; do not copy a raw diff into a model prompt or assume a provider claim is QA proof.
3. Describe what the artifact contains, which acceptance criterion it supports, and what review remains. Mark missing, truncated, failed, or unknown storage as an unresolved handoff rather than a delivered file.
4. If the current run needs QA, use `request_qa_review_v1` only when admitted and report the request ID. The operator converts that request to a linked draft and decides when QA runs.

Return a concise handoff with owner IDs and verified hashes. This procedure does not upload arbitrary files, bind an external chat attachment, send a message, or prove external delivery.
