# design work review

Use this operator-started procedure to triage a project's named design requests, written UX critiques, and user-visible regressions. The operator supplies the design goal, affected users, current requests, and any relevant screenshots or browser evidence as a described evidence packet.

1. Set the review cutoff and distinguish direct observation from a requester's description. Use admitted `recent_project_work_v1` or `run_evidence_v1` for named records; a bounded list is not a complete design backlog.
2. Rank up to three items by user impact, urgency, and whether a decision or evidence gap blocks work. Give each item a next deliverable, owner, acceptance step, and supporting record ID.
3. For a suspected regression, describe the exact flow and state that QA should verify. Treat supplied screenshots or browser reports according to their provenance; do not claim that this role opened a live page.
4. Return a concise review and an explicit next design or QA handoff. An admitted `create_draft_work_order_v1` can prepare a same-project draft. An admitted `request_qa_review_v1` requests QA for the current run only; the operator controls conversion and start.

This procedure does not create wireframes, run a live browser, assign a designer, or schedule weekly reviews.
