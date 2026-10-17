# Browser verification session plan

**Trigger:** A named flow needs real browser evidence.

**Required inputs:** URL or exact local fixture, user flow, viewport list, expected states, and test account handling.

**Capability preflight:** OneFoundry’s `browser_static_inspect_v1` handles only approved loopback HTML/CSS fixtures via a separate bridge; it cannot navigate a live site, fill forms, capture screenshots, or read network/console.

**Procedure:** Turn the flow into repeatable actions and observations. Separate fixture checks from live-site checks, include desktop/mobile and failure recovery, and define evidence IDs and pass criteria.

**Output:** Return a browser test script and `blocked` for live execution unless an authorized browser owner supplies actual results.
