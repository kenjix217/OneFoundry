# Release gate coordination plan

**Trigger:** An operator asks what evidence is needed to promote a candidate release.

**Required inputs:** Candidate commit, channel, change notes, test receipts, deployment environment, rollback plan, and approvals.

**Capability preflight:** OneFoundry has no package release, deployment, canary, or smoke-test action tool. Role results are not release evidence without owner readback.

**Procedure:** Sequence source freeze, change-note review, candidate verification, canary observation, smoke evidence, approval, publication, and post-release readback. Stop on unknown candidate identity or failed gate.

**Output:** Return a gate table with owners, evidence, and current state. Mark promotion and publication blocked until separately authorized and executed.
