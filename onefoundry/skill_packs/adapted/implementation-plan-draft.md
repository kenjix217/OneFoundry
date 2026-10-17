# implementation plan draft

Trigger: The operator has accepted a specification and wants ordered engineering tasks.

Inputs: Approved scope, component owners, baseline revision, constraints, acceptance criteria, and available test environments.

Capability preflight: Check admitted project context with `project_brief_v1` when needed and verify whether source architecture is supplied. `create_draft_work_order_v1` can save one same-project draft when admitted, but cannot mark it ready or launch it; the external coding runner remains a separate gate.

1. Divide the work into smallest independently verifiable changes with explicit dependencies.
2. For each task, name files or owners only when verified, expected artifacts, regression cases, and failure handling.
3. Order migrations, compatibility work, and integration checks before dependent behavior.
4. Present the plan for operator correction; create a draft only on request and report its returned owner record.

Output: An ordered task plan with evidence gates and unresolved decisions. It is not an approved launch instruction or proof that code changed.
