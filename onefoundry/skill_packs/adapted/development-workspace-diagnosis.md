# Development workspace diagnosis

**Trigger:** A developer reports that a local test instance is unreachable, unhealthy, or serving unexpected data.

**Required inputs:** Expected checkout, service URL, process/container identity, health response, logs, and database identity.

**Capability preflight:** Role agents have no local process, Docker, shell, or database inspection tool. Saved run evidence may be read by `run_evidence_v1` when admitted.

**Procedure:** Build a read-only identity chain from supplied process to served URL and data store; distinguish port collision, wrong checkout, setup gate, login failure, and dependency failure. State the leading hypothesis with direct evidence and a discriminating next probe.

**Output:** Return a diagnosis and exact operator/Engineer probes. Mark repair and restart unexecuted.
