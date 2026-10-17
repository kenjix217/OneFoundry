# parallel work candidates

Trigger: The operator asks which independent engineering tasks could proceed concurrently.

Inputs: Named tasks, owner paths, dependencies, shared files and environments, required approvals, and current run states.

Capability preflight: Use admitted `recent_project_work_v1` or `run_evidence_v1` only for bounded status. OneFoundry has no role-agent subagent dispatch or shared-workspace conflict manager; `create_draft_work_order_v1` creates a draft, not a running parallel worker.

1. Map each task's inputs, outputs, and mutable resources.
2. Mark tasks independent only when they have no unresolved sequencing or shared-state conflict.
3. Define a separate acceptance check and integration owner for each candidate.
4. Present a proposed work-order split for operator review; keep ordering explicit for dependent tasks.

Output: A dependency table and suggested assignment order. This procedure does not spawn agents, reserve capacity, start runs, or merge their work.
