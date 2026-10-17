# delegated implementation plan

Trigger: The operator wants a multi-task implementation divided among independent coding workers.

Inputs: Approved specification, task dependencies, repository baseline, boundaries for each worker, review criteria, and integration owner.

Capability preflight: Confirm a provider-backed coding runner with proved isolation, authentication, and spending control; separate worker sessions; immutable task manifests; review dispatch; and integration receipts. OneFoundry currently has no general role-agent subagent loop, and external coding attempts receive skill metadata rather than these procedures.

1. Split only independent tasks and give each an exact input, output, allowed scope, and test requirement.
2. Define a review after each result and a final integrated review that checks interactions.
3. Specify conflict handling and how unknown attempts are reconciled before any retry.
4. If worker or reviewer contracts are absent, stop at a plan and name the missing contracts for operator decision.

Output: A task and review matrix or `blocked_for_delegate_runtime`. This procedure does not spawn workers or certify their results.
