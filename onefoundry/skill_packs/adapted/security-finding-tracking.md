# security finding tracking proposal

Trigger: The operator wants a confirmed finding recorded in an external tracker or private advisory.

Inputs: Finding identity, validated evidence, target project/repository, intended tracker, visibility, assignee, and exact proposed text.

Capability preflight: Check for a selected tracker connector, authorization, deduplication lookup, outbound approval, idempotency key, dispatch receipt, and semantic owner readback. OneFoundry has a bounded GitHub PR comment action, not a finding issue or advisory creation contract.

1. Confirm the finding's status and redact secrets, protected data, and unnecessary exploit detail.
2. Draft one tracker record with affected scope, impact, evidence links, remediation owner, and unresolved questions.
3. Match an existing record only through an admitted owner lookup; do not infer absence from a supplied list.
4. Present exact destination and content for operator approval. If any connector or gate is absent, stop with the missing setup; never post through another action type.

Output: A reviewable tracking proposal or `blocked_for_tracker_contract`. Neither a draft nor approval alone proves that a tracker record was created.
