# PR readiness evidence review

**Trigger:** A reviewer asks whether a named change is ready for human review.

**Required inputs:** Exact repository/PR identity, observed head revision, check and review records, PR body, and diff evidence.

**Capability preflight:** `github_pr_read_v1` can provide a bounded PR snapshot only through a trusted run and one-use approval; it omits checks and discussions. No GitLab or Perforce connector is admitted.

**Procedure:** Compare every supplied check and comment to the exact head; distinguish pending, stale, failing, and resolved observations. Inspect the supplied description for purpose, test evidence, and deployment notes. List each unresolved concern with the next owner check.

**Output:** Return a readiness evidence table and `blocked` when current-head checks or comments are unavailable. Do not wait, fix, resolve threads, or mark ready.
