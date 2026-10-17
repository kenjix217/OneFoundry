# DOCX production intake

Use when a work order requests a native Word document or edit. Inputs: source text, intended audience, required structure, reference file, citations, and formatting constraints.

1. Plan headings, tables, figures, page breaks, and source attribution.
2. List native edits and evidence needed to protect existing content or template fidelity.
3. Define page-by-page render and review checks.

Capability preflight: Require a DOCX authoring/import capability, persistent artifact store, renderer, and page-image review. OneFoundry text roles can draft Markdown but cannot create or visually verify DOCX.

Output: Return a document production brief or blocked result; do not label Markdown a finished Word file. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
