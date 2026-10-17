# Chrome session review brief

Use when a task depends on an already logged-in Chrome tab or extension state. Inputs: target origin, account context, requested observation, permitted interaction, and sensitive-data boundary.

1. Specify the minimum session observations needed and the exact tab to use.
2. Describe a narrow action sequence and what evidence would establish the result.
3. Flag any private data that must be redacted before saving evidence.

Capability preflight: Require operator-approved access to the existing Chrome session and a connected Chrome control tool. OneFoundry has no live Chrome session bridge or extension access.

Output: Return a session-review plan or blocked result; do not imply access to cookies, tabs, or authenticated pages. Save a requested Markdown brief only when `write_text_artifact_v1` is admitted.
