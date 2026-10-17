# Project activity distillation

**Trigger:** An operator supplies a complete, bounded activity bundle for one project and asks for durable synthesis.

**Required inputs:** Project identity, time window, work-order/result/event references, prior summary, and source provenance.

**Capability preflight:** `recent_project_work_v1` is bounded and may not cover the full window; `approved_memory_v1` reads published scoped memory. Roles cannot edit the memory store or fetch arbitrary attachments.

**Procedure:** Read every supplied record in scope, group changes by decision, risk, outcome, and next action, and preserve prior decisions unless superseded by cited newer evidence. Separate current standup from durable project knowledge.

**Output:** Return a cited standup and candidate durable facts for operator review; mark memory publication blocked until its owner accepts them.
