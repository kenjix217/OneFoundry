# agent workflow incident review

Trigger: The operator reports that an engineering agent workflow behaved unexpectedly or consumed excessive time.

Inputs: Exact work order, run IDs, expected procedure, observed outcome, timestamps, and available receipts.

Capability preflight: Check admission to `run_evidence_v1` and `coding_evidence_v1` for named runs. OneFoundry does not expose external coding-host transcripts, hidden prompts, or subagent internals through these tools; request a supplied transcript when those details matter.

1. Build a timestamped sequence from owner records and label operator claims separately.
2. Identify the first decision or transition that diverged from the intended workflow.
3. Distinguish instruction ambiguity, unavailable capability, tool failure, unknown external outcome, and missing evidence.
4. Recommend one process correction and a way to test it; do not rewrite a profile based on a single uncorroborated story.

Output: An evidence-linked incident summary with gaps and a proposed next check. It does not diagnose external coding-host internals or retrieve host session files.
