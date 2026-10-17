# Benchmark iteration plan

**Trigger:** An Engineer wants to improve a named benchmark task over bounded iterations.

**Required inputs:** Benchmark contract, baseline result, exact code revision, test harness, allowed iteration count, and stop rule.

**Capability preflight:** OneFoundry has no Terminal-Bench integration or general shell tool for role agents. Engineer coding runs require the external runner and its safety gates.

**Procedure:** Define one smoke attempt per revision, record observed stop point, distinguish harness failure from product defect, and propose the smallest next experiment. Require operator review before changing product behavior or rerunning an unknown side effect.

**Output:** Return an iteration ledger and next experiment; mark execution and score claims blocked without receipts.
