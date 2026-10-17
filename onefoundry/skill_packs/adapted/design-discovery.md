# design discovery brief

Trigger: The operator proposes a new feature or behavior and wants a decision-ready design before implementation.

Inputs: Desired outcome, users, current behavior, constraints, project context, and unresolved decisions.

Capability preflight: `project_brief_v1` may supply bounded project context when admitted. Check whether source, UX, and dependency evidence is supplied; live text roles cannot browse private application state or run code. Missing facts become questions, not invented design assumptions.

1. Restate the intended result and measurable acceptance criteria.
2. Identify the component owner, likely data and tool boundaries, and key tradeoffs from available evidence.
3. Compare a small number of feasible approaches and select one with explicit assumptions and risks.
4. Present the design for operator correction before proposing a draft work order; `create_draft_work_order_v1` requires separate saved and current permission.

Output: A concise design decision and unresolved-question list. This procedure does not authorize implementation, approval, or automatic delegation.
