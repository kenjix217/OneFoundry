# project tracker template intake

Use this procedure when a work order selects the project tracker reference for a project tracking workbook.

Inputs: the approved reference or its exact identifier, work IDs, owners, milestones, status rules, and dependencies.

1. Confirm the requested audience, decision, source facts, and which reference structure must be retained. Mark absent facts as gaps rather than filling template slots with guesses.
2. Map the supplied material to the reference sections or sheets. List any fields, figures, or formulas that require owner verification before authoring.
3. Prepare a content and verification brief covering row identity, validation, rollups, and stale-status cues. Record intentional departures from the reference.

Capability preflight: check that the exact retained reference is available to an authorized artifact path, that a XLSX authoring/import tool can preserve its native structure, and that a renderer and visual review path are available. OneFoundry text roles currently have none of those native artifact capabilities. If any gate is absent, return the preparation brief with the missing capability and next owner action; do not claim a completed XLSX file.

Output: a source-grounded, render-ready spreadsheet brief or a precise blocked result. `write_text_artifact_v1` may save requested Markdown or CSV only when admitted; it does not create or inspect the native template.
