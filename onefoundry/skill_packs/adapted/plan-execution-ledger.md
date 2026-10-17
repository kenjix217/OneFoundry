# plan execution ledger

Trigger: The operator has an approved multi-step implementation plan and wants a concrete execution record.

Inputs: Plan version, ordered tasks, acceptance criteria, named owner, baseline revision, and available test commands or receipts.

Capability preflight: `current_work_order` and `run_evidence_v1` can expose admitted owner state. Check for an authorized external coding runner before expecting source edits or tests; OneFoundry's text role cannot execute a plan. A draft work order is not a launch.

1. Break the approved plan into verifiable checkpoints without changing its accepted scope.
2. For each checkpoint, record the expected artifact, test, owner readback, and dependency on earlier steps.
3. Update the ledger from saved receipts only; mark provider claims and missing checks separately.
4. When a step fails or becomes unknown, pause dependent steps and name the needed reconciliation or plan decision.

Output: A checkpoint ledger and next authorized action. No code execution, automatic retry, or completion claim occurs through this procedure.
