# Work-order interaction surface plan

**Trigger:** An operator asks for a new user or agent interaction on a work-order timeline.

**Required inputs:** Interaction purpose, actors, visibility, state transition, event contract, and existing UI/API evidence.

**Capability preflight:** OneFoundry has work-order, event, approval, and role-tool owners; a bound procedure cannot add routes, components, or permissions.

**Procedure:** Define the smallest shared payload and validation rule, then map server ownership, UI placement, approval behavior, and event readback. Identify one normal, invalid, and stale-state test plus accessibility checks.

**Output:** Return a scoped implementation brief with contract and acceptance criteria. Route code work to Engineer; claim no UI was created.
