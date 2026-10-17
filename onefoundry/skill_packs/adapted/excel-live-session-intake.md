# live Excel session intake

Use when a request targets an already open Microsoft Excel workbook through a connected session. Inputs: exact workbook identity, selected range, requested change, formulas and dependencies, account/session state, and approval.

1. Confirm the workbook and sheet identity before proposing edits.
2. Describe exact read, write, recalculation, and postchange verification ranges.
3. Record how session loss or uncertain write outcome is handled with owner readback.

Capability preflight: Require the connected Excel session and range read/write/image tools. OneFoundry has no ChatGPT Excel add-in control or live workbook registration.

Output: Return a session setup and edit plan or blocker; never claim workbook cells changed. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
