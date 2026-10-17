# typed visualization contract

Use when a TypeScript chart implementation needs a safe data and component contract. Inputs: input schema, units, missing-value rules, rendering target, events, and consumer API.

1. Define typed raw and normalized records, validation boundary, and transform ownership.
2. Describe component props, events, error states, and renderer separation.
3. Give compile, fixture, interaction, and visual QA checks.

Capability preflight: Require an authorized code workspace, TypeScript compiler, relevant tests, and browser QA to implement. OneFoundry role text cannot compile or inspect the page.

Output: Return a typed contract and test plan, not a compiled component. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
