# code review request brief

Trigger: The operator wants another reviewer to inspect a named engineering change.

Inputs: Immutable patch identity, base and head, expected behavior, risk areas, completed tests, known gaps, and intended reviewer.

Capability preflight: Check for a complete diff and available reviewer assignment path. `request_qa_review_v1` requests QA for the current active run only and requires saved/current permission; it does not dispatch a code reviewer subagent or review an earlier run automatically.

1. Define the exact change and contracts the reviewer should check.
2. Give the reviewer enough baseline, failure, and test context to challenge the change independently.
3. Separate required review from optional advice and name the acceptance owner.
4. If the assignment path is missing, return a review brief for the operator to route manually.

Output: A concise review request or `blocked_for_reviewer_dispatch`. This procedure does not spawn a reviewer or count an unreturned request as review evidence.
