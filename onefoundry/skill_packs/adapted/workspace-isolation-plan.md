# workspace isolation plan

Trigger: The operator proposes simultaneous engineering work that might require a separate checkout.

Inputs: Repository, current checkout status, intended task, shared files, existing runner isolation, and the operator's workspace preference.

Capability preflight: Verify an approved workspace creation and cleanup owner, exact repository identity, storage bounds, and branch permissions. OneFoundry's disposable external runner is separate from external coding-host Git worktrees. KJ31 requires explicit advance approval before creating or moving implementation into a separate Git worktree.

1. Determine whether existing disposable runner isolation already covers the work.
2. Identify collisions with uncommitted files, branches, services, or credentials in the requested checkout.
3. Propose the exact checkout or worktree location, baseline, cleanup conditions, and approval needed if isolation is required.
4. Stop before creating or switching anything until the named owner authorizes and executes it.

Output: An isolation decision record or `blocked_for_workspace_approval`. This procedure never creates a worktree, moves work, or cleans a checkout.
