# procedure selection

Trigger: The operator asks which OneFoundry procedure should guide a named work order.

Inputs: Work-order goal, role/profile, available Skill Library entries, allowed tool IDs, and project constraints.

Capability preflight: Confirm the exact procedure version exists and is bound to the profile, and that each needed tool is permitted in both saved and current profile state. A procedure does not grant tools, invoke another procedure automatically, or change external host skill settings.

1. Match the task to one narrow procedure based on its trigger and evidence needs.
2. Identify overlap or conflicting instructions and prefer the project owner's explicit scope.
3. List capability gaps and propose a manual or preparation-only path where tools are absent.
4. Present the selected version and tool requirements for operator review before changing bindings.

Output: A procedure selection rationale and setup checklist. This is OneFoundry library guidance, not external skill invocation or automatic profile mutation.
