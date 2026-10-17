# extension package plan

**Trigger:** An engineer is exploring a new external tool or plugin integration.

**Required inputs:** Capability goal, trust boundary, external service API, credential owner, and existing Tool Gateway contract.

**Capability preflight:** OneFoundry has no general plugin installer or MCP action grant. A role can research and propose; code and connector installation require separate engineering work.

**Procedure:** Identify the narrow tool contract, input/output validation, project scoping, approval classification, secret handling, and event/readback design. Compare with an existing tool before proposing a new one.

**Output:** Return an extension plan and missing capability list; do not scaffold or install an external plugin.
