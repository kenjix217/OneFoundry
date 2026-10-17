# D3 and SVG render plan

Use when a custom vector chart needs D3 scales, axes, or annotated SVG. Inputs: data schema, chart claim, interaction needs, mark count, labels, and export target.

1. Define transforms and scale domains with units and missing-value policy.
2. Specify SVG structure, annotation placement, keyboard and text alternatives.
3. List responsive, data-correctness, and export checks.

Capability preflight: Require a coding environment with D3, a browser renderer, and visual QA before claiming implementation. OneFoundry role text output is only a specification.

Output: Return an SVG/D3 implementation brief and open checks. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
