# first priority review

Use this operator-started procedure at the beginning of a project or a new planning cycle to choose the next useful, reviewable work order. The operator supplies the company goal, project scope, and any current ownership or deadline constraints.

1. Name the project and review cutoff. Read `project_brief_v1` and `recent_project_work_v1` only when admitted; their bounded results may omit older work. Request missing goal or workspace context from the operator instead of inventing it.
2. Separate completed outcomes, active work, approval waits, failed or unknown runs, and unassigned ideas. Use `run_evidence_v1` for a named result when available. Do not treat a status label as proof of completion.
3. Recommend one next deliverable with an owner profile, acceptance evidence, and the reason it outranks alternatives. Identify whether a Researcher, Engineer, and QA handoff is needed, without assuming those runs have started.
4. Return a short account of what changed, the selected next action, blockers, and operator decisions. If admitted, `create_draft_work_order_v1` may create a same-project draft for operator review; it does not make the order ready or launch it.

This is a snapshot, not an autonomous heartbeat or recurring schedule. Project creation, profile assignment, and coding-provider launch remain operator-owned actions.
