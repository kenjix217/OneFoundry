# dashboard behavior plan

Use this procedure to specify a dashboard from a supplied operating decision, measures, update cadence, and failure requirements. Inputs are metric definitions, data owners, freshness expectations, audience, devices, and any existing screen description.

1. Identify the first decision a viewer should make and the few measures needed for it. Define each measure's grain, unit, source, and missing-data meaning.
2. Describe the initial reading order, filters, drill-down, and coordinated chart behavior without inventing a data feed.
3. Specify stale, disconnected, partial, loading, empty, and reconnect states. Include a mobile first view and a text/table alternative.
4. Give testable acceptance checks for value correctness, freshness, interaction state, and performance assumptions.

Return a concise dashboard behavior contract, not a rendered dashboard or live metric verdict. `write_text_artifact_v1` can save a requested Markdown spec when admitted; no streaming data, chart renderer, screenshot, or browser QA is provided by this procedure.
