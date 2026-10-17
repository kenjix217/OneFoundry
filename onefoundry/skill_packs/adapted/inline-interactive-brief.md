# interactive visual brief

Use when a question would benefit from an interactive explainer, simulator, or adjustable chart inside a host conversation. Inputs: concept, data and units, intended controls, initial state, accessibility needs, and device constraints.

1. Define the viewer question and the smallest useful interaction.
2. Specify controls, state transitions, validation, reset, and a static text fallback.
3. List tests for boundary inputs, mobile use, keyboard access, and claim accuracy.

Capability preflight: Require an inline HTML rendering host or an approved standalone app runtime with browser QA. OneFoundry text artifacts do not display interactive HTML and no general renderer is admitted.

Output: Return an interaction specification or blocked result; do not claim an embedded visual ran. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
