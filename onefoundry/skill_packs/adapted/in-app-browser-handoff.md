# in-app browser handoff

Use when a work order asks for a review in an existing in-app browser tab. Inputs: exact tab or URL, visible task, permitted actions, expected states, and device sizes.

1. Define the tab identity and initial state that an operator must confirm.
2. Write a short sequence of interactions and evidence to capture at each state.
3. Record how login, redirects, or tab loss would stop the review.

Capability preflight: Require an authenticated in-app browser control capability and tab-state readback. OneFoundry has no such role tool; its static fixture path does not access an external browser tab.

Output: Return an operator-ready handoff with the exact blocked capability; do not claim the tab was opened or controlled. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
