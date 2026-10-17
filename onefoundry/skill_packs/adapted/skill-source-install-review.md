# skill source install review

Trigger: The operator proposes adding a third-party procedure to the OneFoundry Skill Library.

Inputs: Source identity and version, license, original content location, desired project and profile, tool needs, and proposed adaptation.

Capability preflight: Verify source provenance and terms, the OneFoundry project Skill Library API, exact-version readback, and operator-selected binding. External skill installers and GitHub download scripts install host skills, not OneFoundry procedures. No source text or scripts may be copied merely because the source is listed in an inventory.

1. Evaluate whether an existing OneFoundry procedure already covers the need.
2. Separate reusable guidance from host commands, secrets, connectors, and ungranted permissions.
3. Draft an original bounded procedure with trigger, inputs, steps, output, and missing-tool outcome.
4. Present the source mapping and license decision before any reviewed import; bind only an exact saved version through the operator path.

Output: A source-to-procedure review and import prerequisites or `blocked_for_license_or_import`. This procedure does not download, install, import, or bind anything.
