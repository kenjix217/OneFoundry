# branch finish review

Trigger: The operator requests a readiness review for a completed engineering branch.

Inputs: Branch and exact head revision, target branch, change summary, test receipts, QA verdict, pending approvals, and merge policy.

Capability preflight: Verify access to a complete comparison, current branch head, required checks, and merge owner readback. OneFoundry has no general Git branch or merge action; its approved GitHub stage proposal is not a merge. `coding_evidence_v1` supplies bounded receipt metadata only.

1. Confirm the claimed head and target are current through an admitted owner source, or mark them unverified.
2. Compare requirements to saved diff, tests, and QA evidence; distinguish missing gates from failures.
3. Identify unresolved conflicts, external side effects, and rollback needs.
4. Present readiness and exact operator choices, with merge as a separate approval-bound action requiring its own contract.

Output: `ready_for_owner_decision`, `needs_work`, or `unknown` with evidence IDs. This procedure cannot switch branches, commit, push, open a PR, or merge.
