# External task bridge contract

**Trigger:** An operator wants another assistant or service to create or update OneFoundry work orders.

**Required inputs:** External caller identity, target project, operation list, credential plan, idempotency key, and expected readback.

**Capability preflight:** OneFoundry has no Hermes bridge or general external task API credential for agents. Tool Gateway owns admitted work-order actions; `create_draft_work_order_v1` is same-project draft only.

**Procedure:** Define each allowed operation, actor attribution, project scope, validation, duplicate handling, and approval boundary. Specify how an unknown network result is reconciled before retry.

**Output:** Return a bridge contract and blocked capability list; do not create keys or transmit tasks.
