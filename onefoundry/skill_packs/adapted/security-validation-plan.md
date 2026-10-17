# security candidate validation plan

Trigger: The operator asks whether a specific security candidate is real.

Inputs: Candidate claim, exact revision, alleged input-to-sink path, policy boundary, source evidence, and safe test environment description.

Capability preflight: Check for complete source-path reads and an authorized isolated test runner before attempting validation. `repository_file_v1` reads exact saved files for trusted callers only; OneFoundry has no general security-test execution tool or dedicated security-validation service.

1. State attacker control, required preconditions, target authority, and expected sensitive effect.
2. Trace each known control along the path and identify the first missing or ineffective enforcement point.
3. Design a safe discriminating test and a legitimate control case; label them unexecuted unless a trusted receipt exists.
4. Classify the candidate as supported, contradicted, or unresolved within inspected evidence, and name the next missing observation.

Output: An evidence-bound validation plan and provisional verdict. Do not call a candidate reproduced or validated from a proposed test or scanner label.
