# security policy scope draft

Trigger: The operator asks to define or review security guidance for a named repository component.

Inputs: Repository, revision, component path, supplied policy files, architecture and trust boundaries, and the operator's desired review scope.

Capability preflight: Confirm whether a trusted run-bound caller can read each exact saved policy file through `repository_file_v1`. It cannot discover every nested policy; live text SDK cannot receive raw repository content. If the policy chain is incomplete, request its exact files or return a partial draft.

1. Map the component's assets, entry points, authority checks, external dependencies, and sensitive sinks from supplied facts.
2. Separate root guidance from narrower component guidance and note conflicts that require owner resolution.
3. Draft testable security properties, meaningful finding criteria, and explicit exclusions without adding secrets or exploit instructions.
4. Mark every unsupported architecture claim and missing policy path; propose an owner check before treating the draft as repository policy.

Output: A reviewable policy outline with source references, open questions, and an `incomplete_policy_chain` marker when needed. This procedure does not edit `SECURITY.md`, discover all policies, or activate a scanner.
