# Legacy runtime contract mapping

**Trigger:** An engineer encounters an older external execution procedure during OneFoundry design.

**Required inputs:** Specific historical procedure version, intended capability, and current OneFoundry owner path.

**Capability preflight:** The legacy heartbeat, issues, and API endpoints do not exist in OneFoundry. Tool Gateway permissions and the Execution Gateway determine current authority.

**Procedure:** Extract the historical intent, map it to OneFoundry project/work order/run/result/event owners, and list API assumptions that cannot transfer. Check handoff and approval semantics separately.

**Output:** Return a migration map with `supported`, `proposal`, or `unavailable` per action. Do not invoke legacy endpoints.
