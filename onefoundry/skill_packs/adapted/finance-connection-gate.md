# Spend-tool connection gate

**Trigger:** An operator asks how a finance platform playbook could be used.

**Required inputs:** Named provider/action, data fields, intended spending or account effect, operator approvals, and credential owner.

**Capability preflight:** OneFoundry has no Ramp connector or finance account tool. Financial, account, card, and spending actions require explicit operator approval and separately admitted capability.

**Procedure:** Identify read-only versus side-effect steps, required scopes, amount limits, reconciliation source, and secret handling. Draft the approval text and failure/unknown-outcome process before any connector work.

**Output:** Return a connection and approval plan; mark every live finance action blocked.
