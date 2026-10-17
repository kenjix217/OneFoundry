# Cited knowledge answer

**Trigger:** An operator asks a factual question about a project’s saved knowledge.

**Required inputs:** Question, project identity, allowed memory scope, and exact supplied source pages when needed.

**Capability preflight:** `approved_memory_v1` is available only under saved and current permission; it may return a bounded subset. There is no wiki page/raw-source connector or automatic synthesis filing.

**Procedure:** Search admitted memory, follow citations present in the supplied packet, verify material claims against original source excerpts where available, and lead with a direct answer. Identify the exact missing source for each unsupported claim.

**Output:** Return a cited answer or `insufficient evidence`, with a proposed durable synthesis topic if useful. Do not write a wiki page.
