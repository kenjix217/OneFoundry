# security diff scan preparation

Trigger: The operator requests security review of one PR, commit range, or immutable patch.

Inputs: Exact base and head, patch hash or provider comparison identity, changed-file inventory, relevant security policy, and requested scope.

Capability preflight: Check for a complete immutable diff, baseline and head file reads, deleted-file access, candidate and coverage ledger, and a scan finalizer. Current `coding_artifact_text_v1` may expose one verified saved diff to a trusted caller but does not guarantee a complete comparison. OneFoundry has no dedicated security diff-scan workbench.

1. Bind the intended comparison and detect missing, truncated, or mutable patch evidence.
2. List changed trust boundaries, deleted behavior, and unchanged callers that a full reviewer would need.
3. Identify which files and security checks cannot be covered with current reads.
4. Prepare the exact scan request or, when the full scan contract is absent, return the missing contracts and a narrower manual-review option.

Output: Comparison identity, coverage plan, and `blocked_for_scan_service` unless the scan service actually returns a final receipt. Never label a patch risk brief a completed security diff scan.
