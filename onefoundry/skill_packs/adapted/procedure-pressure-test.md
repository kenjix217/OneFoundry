# procedure pressure-test plan

Trigger: The operator asks whether a proposed OneFoundry procedure gives reliable guidance before binding it to a role.

Inputs: Draft procedure, intended role, representative normal and adversarial work orders, tool allowlist, and expected safe behavior.

Capability preflight: Check for a disposable role-run test harness and a way to compare behavior with and without the draft. OneFoundry has versioned Skill Library storage, but no installed automated subagent pressure-test service. Do not run live external actions merely to test wording.

1. Define cases that expose ambiguity, unavailable tools, misleading evidence, and conflicting requests.
2. Record baseline behavior where a test harness exists; otherwise mark it unexecuted.
3. Revise the draft to close observed gaps while keeping procedure text separate from permissions.
4. Require a reviewed version and owner-selected binding; a test-only result does not update active profile pins.

Output: A case matrix, observed or unexecuted results, and revision recommendation. This procedure does not install skills, create subagents, or prove a procedure effective without actual test receipts.
