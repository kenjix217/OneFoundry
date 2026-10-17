# Knowledge consistency audit

**Trigger:** An operator supplies an index, linked pages, and sources for one knowledge scope.

**Required inputs:** Complete scoped page list, current index, citations, source dates, and change log.

**Capability preflight:** OneFoundry lacks wiki search/read/log tools. `approved_memory_v1` is scoped memory search, not a complete page graph; a partial packet cannot support a clean lint verdict.

**Procedure:** Check the supplied packet for contradictory claims, stale sources, missing links, unreferenced pages, circular citations, and index drift. Group findings by severity with exact page and evidence references.

**Output:** Return a triage list or explicitly partial audit, plus suggested owner checks. Do not rewrite pages or append a log.
