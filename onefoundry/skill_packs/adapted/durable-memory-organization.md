# Durable memory organization proposal

**Trigger:** An operator wants to retain and retrieve project facts across runs.

**Required inputs:** Candidate facts, source IDs, project scope, sensitivity, prior published memory, and supersession evidence.

**Capability preflight:** OneFoundry memory is PostgreSQL-backed and scope-filtered; `approved_memory_v1` reads published facts under saved/current scope. Roles cannot write `$AGENT_HOME` files, run `qmd`, or publish memory through this procedure.

**Procedure:** Separate transient run notes, durable facts, and operator preferences. Propose atomic records with source, date, scope, and supersession links; flag ambiguous ownership or private data for review.

**Output:** Return candidate memory entries and a retrieval plan. Mark actual publication and filesystem PARA organization unavailable.
