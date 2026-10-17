# deep security scan preparation

Trigger: The operator requests repeated, independent security review of one repository scope.

Inputs: Project, exact repository and revision, scope, authorized security context, relevant policy, available review budget, and requested coverage.

Capability preflight: Check for a dedicated scan coordinator, independent worker dispatch, immutable scan identity, coverage ledger, artifact store, and final report owner. OneFoundry currently has no dedicated deep-scan service. `repository_file_v1` cannot inventory a repository, and a role instruction cannot start scan workers.

1. Freeze the proposed scope and distinguish whole-repository coverage from selected paths.
2. Define how independent passes would preserve the same scope while recording their separate evidence and disagreements.
3. List the required worker count, time limit, cancellation behavior, deduplication rule, and final owner readback as requirements, not completed actions.
4. If the coordinator is absent, prepare a scan request with the missing capability and handoff owner. Do not substitute one text response for repeated scans.

Output: A bounded scan plan and `blocked_for_scan_service` outcome until the service and its receipts exist. Never claim a deep scan, validated finding, coverage percentage, or generated report from this procedure.
