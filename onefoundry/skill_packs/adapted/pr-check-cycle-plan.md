# PR check-cycle diagnosis

**Trigger:** A named PR has failing, missing, or pending checks.

**Required inputs:** Exact head SHA, check names/statuses with timestamps, logs or failure excerpts, and test ownership.

**Capability preflight:** No role tool polls CI or changes code. `github_pr_read_v1` cannot supply check runs; Engineer execution uses a separate runner only after its gates.

**Procedure:** Tie every check to the exact head, separate infrastructure noise from code failure, and propose one reproducible probe per failure. On a new head, require fresh evidence rather than carrying over an old pass.

**Output:** Return a bounded check-cycle plan or `blocked` for absent current-head evidence; claim no CI was run or fixed.
