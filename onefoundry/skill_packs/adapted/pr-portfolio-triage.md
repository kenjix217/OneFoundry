# Owned PR portfolio triage

**Trigger:** An operator wants to prioritize several PRs their team owns.

**Required inputs:** List of exact PR IDs, owners, heads, checks, reviews, blockers, and deadlines.

**Capability preflight:** OneFoundry cannot discover a PR portfolio or monitor checks. `github_pr_read_v1` is one approved bounded snapshot per trusted run and lacks review status.

**Procedure:** For each supplied PR, separate stale-head evidence, actionable feedback, dependency block, and merge decision. Rank next reviewer or author actions. Identify duplicate follow-up work before suggesting drafts.

**Output:** Return a portfolio table with observation times; do not push, merge, archive inbox items, or promise continued monitoring.
