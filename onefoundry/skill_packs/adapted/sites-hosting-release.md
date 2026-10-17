# Sites hosting release brief

Use when a completed Sites project needs a publishing decision or release plan. Inputs: exact project, candidate version, target domain, approval owner, acceptance evidence, and rollback target.

1. Identify the candidate bytes, current publication, and intended audience.
2. List required prepublish checks and an operator approval record for the exact target.
3. Define postpublish readback and rollback evidence, including unknown-outcome handling.

Capability preflight: Require a connected Sites hosting tool, exact candidate identity, and explicit outbound publication approval. OneFoundry has no Sites hosting connector or live deployment readback.

Output: Return a release checklist or blocked result; never report publication from an approval or plan alone. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
