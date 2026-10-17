# spreadsheet production intake

Use when a work order requests native workbook creation, modification, or analysis. Inputs: exact source workbook or data, sheet structure, formulas, units, outputs, and template constraints.

1. Define tabs, input/output cells, formula lineage, validation, and scenario assumptions.
2. Separate source values from derived values and identify reconciliation checks.
3. Plan recalculation, range rendering, and affected-sheet visual review.

Capability preflight: Require an admitted XLSX importer/author, formula calculator, persistent artifact store, and renderer. OneFoundry can save text or CSV only and cannot verify a native workbook.

Output: Return a workbook specification or blocked result; never claim recalculation or formula preservation. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
