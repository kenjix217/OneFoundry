# PR submission preparation

**Trigger:** An Engineer run has a candidate change that may be submitted for review.

**Required inputs:** Bound source revision, candidate commit, diff/receipt, tests, target branch, and PR template requirements.

**Capability preflight:** `coding_evidence_v1` exposes receipt metadata; raw diff reads require a trusted path. Roles cannot commit, rebase, push, or open a PR through Tool Gateway.

**Procedure:** Check scope, unrelated changes, target/base identity, test proof, documentation, and risks. Draft a PR title/body that cites saved evidence and identifies unexecuted checks.

**Output:** Return a submission checklist and draft body. Mark Git operations and PR creation for an authorized operator or coding provider.
