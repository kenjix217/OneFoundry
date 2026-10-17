# Roster-era runtime contract mapping

**Trigger:** An engineer reviews an external roster-era execution model for possible OneFoundry reuse.

**Required inputs:** Specific roster-era source version, agent hierarchy, work allocation, and required capability.

**Capability preflight:** OneFoundry profiles have saved/current tool permissions and project-scoped work orders; it has no external roster heartbeat or routine dispatcher.

**Procedure:** Compare role ownership, delegation, project boundary, and state transitions with OneFoundry’s current contracts. Identify any ambiguous handoff, duplicate launch, or stale authorization risk.

**Output:** Return a gap matrix and proposed tests; mark roster deployment and heartbeat actions unavailable.
