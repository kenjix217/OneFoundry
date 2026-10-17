# browser acceptance plan

Trigger: The operator requests real-browser verification of a user-visible engineering change.

Inputs: Build and URL, test account or safe fixture, exact flows, desktop and mobile viewports, expected owner state, and allowed external actions.

Capability preflight: Check for an authorized real-browser runner, navigation and screenshot contracts, credential isolation, output bounds, and artifact readback. `browser_static_inspect_v1` handles approved static loopback fixtures and bounded text; it cannot navigate a live app, capture screenshots, or perform a Playwright session. An external `playwright-cli` wrapper is not an OneFoundry role tool.

1. Define repeatable clicks, inputs, expected page state, and backend owner readback for each flow.
2. Specify desktop and mobile widths, accessibility observations, console errors, and screenshot capture points.
3. Identify approval-bound actions and safe test data before any browser execution.
4. If the browser contract is absent, return the manual steps and mark every browser observation unexecuted.

Output: A browser test script and evidence checklist or `blocked_for_real_browser`. This procedure cannot claim visual or end-to-end acceptance without actual browser receipts.
