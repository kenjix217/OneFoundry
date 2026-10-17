# Source knowledge ingestion proposal

**Trigger:** An operator asks to turn one supplied source into reusable project knowledge.

**Required inputs:** Complete source text, source identity/date, project and memory scope, existing relevant memory, and citation rules.

**Capability preflight:** `approved_memory_v1` can read admitted published memory; role agents cannot write approved memory or a wiki graph. Do not open linked attachments or URLs without an admitted tool.

**Procedure:** Read the supplied source end to end, extract a short source summary and atomic claims, flag conflicts with existing facts, and propose entity/concept links only when evidence supports them. Keep source quotations brief and cite the original input.

**Output:** Return a source brief, candidate memory facts, and conflicts. Mark ingestion and index updates as operator-owned, not completed.
