# visual accessibility brief

Use this procedure to draft accessibility guidance for a supplied chart, diagram, or interface description. A Researcher or QA run may also use the text result of an operator-approved `browser_static_inspect_v1` fixture when it is admitted. That tool inspects static HTML/CSS through an operator-dispatched host bridge; it is not live-site navigation, screenshot review, or assistive-technology testing, and it is excluded from live text SDK continuation.

1. Identify the visual's purpose, main claim, data source, intended audience, interactions, and the supplied evidence. Ask for missing labels, values, or state changes when an accurate text alternative depends on them.
2. Draft a short text alternative for the main point. For a complex chart or diagram, add a longer description that preserves categories, relationships, quantities, caveats, and source context. Do not infer unseen pixels or values from a page title or extracted text.
3. Review whether essential meaning survives without color, hover, pointer-only controls, motion, or a wide screen. Propose direct labels, a keyboard path, reduced-motion behavior, and a text or table fallback as applicable.
4. Separate observed defects from design recommendations and untested checks. Name the exact desktop, mobile, keyboard, screen-reader, and export checks still required.

Return the draft and a short verification checklist. Save a Markdown artifact only when `write_text_artifact_v1` is admitted and requested. Do not claim WCAG conformance or completed visual QA from this procedure alone.
