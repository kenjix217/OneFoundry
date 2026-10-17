# visualization test plan

Use this procedure when QA or an operator needs a test plan for a described chart, dashboard, map, or diagram. Inputs are the visual specification, data contracts, expected interactions, target devices, and any actual test evidence.

1. Separate checks for source values and transformations, visual semantics, interactions, accessibility, and responsive layout.
2. Define a small deterministic fixture set covering normal, empty, invalid, missing, extreme, and stale data where relevant.
3. Give concrete expected outcomes for labels, scales, units, filters, focus, keyboard use, and mobile states. Identify what requires screenshot or human visual review.
4. Report supplied test results separately from unexecuted checks and defects.

Return an executable checklist with evidence needed for each check. This text procedure does not run tests, take screenshots, render visuals, or establish browser acceptance. An admitted `browser_static_inspect_v1` result, if supplied, covers only its approved static fixture; it does not prove a live dashboard. Save Markdown only with admitted `write_text_artifact_v1` when requested.
