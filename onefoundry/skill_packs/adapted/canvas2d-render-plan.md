# Canvas2D render plan

Use when dense or frequently redrawn marks suggest a Canvas2D visualization. Inputs: mark count, update cadence, data shape, interactions, export needs, and target devices.

1. Define the draw layers and data-to-pixel mapping.
2. Plan hit testing, high-DPI scaling, labels, semantic text alternative, and redraw invalidation.
3. Specify performance and visual acceptance fixtures.

Capability preflight: Require a code workspace, Canvas-capable browser, performance measurement, and screenshot/interaction QA to implement. OneFoundry has none of these as live text role tools.

Output: Return a rendering architecture and test plan, not a chart or benchmark result. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
