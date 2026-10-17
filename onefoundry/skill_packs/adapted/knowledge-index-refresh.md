# Knowledge index reconciliation

**Trigger:** An operator supplies a bounded knowledge-page inventory and asks for an index refresh proposal.

**Required inputs:** Current index, complete page list for one named scope, titles or lead summaries, and recent change log.

**Capability preflight:** OneFoundry has approved memory reads, not a wiki tree or index write tool. `write_text_artifact_v1` may save a proposed Markdown index but cannot replace a live wiki page.

**Procedure:** Compare current entries with actual supplied pages, list additions/removals, keep custom categories, and give one source-grounded summary per page. Record reconciliation counts and flag pages without a reliable summary.

**Output:** Return a proposed index plus add/remove counts; mark live index and log updates blocked.
