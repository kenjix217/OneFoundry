# security fix brief

Trigger: The operator requests an implementation brief for one plausibly validated security defect.

Inputs: Finding, affected revision, attacker path, failed security property, legitimate behavior, and available regression evidence.

Capability preflight: Establish whether exact source and diff reads are available to a trusted run-bound caller and whether the external coding runner has proved isolation, authentication, and spending control. OneFoundry's live text route cannot edit code or run tests; a saved coding receipt is not a live runner capability.

1. Recheck that the supplied evidence reaches a real sensitive sink and identify the earliest broken owner contract.
2. State the smallest boundary change that would block the path while preserving authorized behavior and compatibility.
3. Specify regression cases for exploit preconditions, legitimate use, failure, and relevant authorization scope.
4. Prepare a work order for an authorized Engineer; require diff, test, and owner readback after implementation. If coding capability is unavailable, stop at the brief.

Output: A scoped fix specification or `blocked_for_coding_runner`. This procedure does not modify code, prove remediation, or authorize deployment.
