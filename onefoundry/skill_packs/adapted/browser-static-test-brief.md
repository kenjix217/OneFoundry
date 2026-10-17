# browser test brief

Use when a work order needs a repeatable browser test of a local page or interaction. Inputs: URL, expected user flow, selectors or labels, viewport, allowed account state, and success criteria.

1. List navigation and interaction steps with the observation expected after each.
2. Separate static markup checks from stateful navigation, screenshots, and real user outcomes.
3. Specify failure capture: URL, visible state, console or network symptom, and safe readback.

Capability preflight: For actual browser execution require an admitted browser runner bound to the target and run, with permitted navigation, screenshot storage, and evidence readback. OneFoundry currently exposes only approved static-fixture text inspection outside live SDK continuation.

Output: Return a test script and exact unexecuted gates, or a bounded account of supplied evidence; never claim clicks or screenshots occurred. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
