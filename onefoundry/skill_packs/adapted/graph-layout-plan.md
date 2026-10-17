# graph layout plan

Use this procedure to plan a readable node-link view from a supplied set of entities and relationships. Inputs are node and edge types, direction, size, hierarchy or constraints, the user task, and any existing layout problems described in text.

1. Identify whether the data is a tree, dependency graph, flow, network, or another graph family. State which relationships are confirmed and which are inferred.
2. Recommend a layout family, ordering or port constraints, edge routing, label priority, and a fallback for dense graphs.
3. Define selection, search, focus, pan and zoom, keyboard navigation, and a mobile path only to the extent the task needs them.
4. Give semantic and visual acceptance checks, including edge direction, overlap, stability after updates, and text fallback.

Return a layout specification and a small test checklist. Do not claim a layout engine was run, pixels were inspected, or browser QA passed. Save Markdown only when `write_text_artifact_v1` is admitted and requested.
