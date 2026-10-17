# PDF production intake

Use when a work order requests PDF extraction, forms, creation, or visual inspection. Inputs: exact PDF or source content, intended operation, page range, form fields, and output requirements.

1. Identify whether the task is text extraction, form fill, layout creation, or page review.
2. List page-level source and field checks, including signatures or protected content where relevant.
3. Specify render, text-layer, and visual acceptance evidence.

Capability preflight: Require an admitted PDF reader/writer and page renderer with artifact readback. OneFoundry has no arbitrary PDF import or native PDF tool for text roles.

Output: Return an operation plan or blocker; do not claim pages or fields were inspected. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
