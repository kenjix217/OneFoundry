# declarative chart plan

Use when a chart is best expressed as data, encodings, transforms, and scales rather than custom drawing. Inputs: tabular fields, types, units, chart claim, filters, accessibility, and intended declarative library.

1. Define field mapping, transforms, aggregation, scales, legends, and tooltip semantics.
2. Check whether the grammar supports required interaction and data volume.
3. Specify schema validation and rendered output tests.

Capability preflight: Require an admitted code/artifact workspace, a chosen declarative renderer, and visual/browser QA to execute a chart spec. OneFoundry has no such role runtime.

Output: Return a declarative specification in prose and a blocked implementation gate; no rendered chart claim. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
