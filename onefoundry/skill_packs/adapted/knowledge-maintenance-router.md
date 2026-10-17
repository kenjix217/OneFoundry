# Knowledge maintenance routing

**Trigger:** A project has a knowledge request that could be a question, source ingest, project distillation, index repair, or integrity audit.

**Required inputs:** Request text, project/scope identity, available source inventory, and desired durable outcome.

**Capability preflight:** OneFoundry has no LLM Wiki plugin or its page-write tools. Admitted memory/context reads can support a bounded answer; writes require the memory owner or operator.

**Procedure:** Classify the request into one operation, identify its required source packet, check whether it is complete, and route to the matching bounded procedure. Preserve source immutability and project scope.

**Output:** Return the operation, evidence checklist, and next owner. Mark wiki maintenance blocked when its tools or source packet are absent.
