# plugin package plan

Trigger: The operator wants a reusable connector or plugin package for a specific OneFoundry capability.

Inputs: Intended users, provider, permissions, tool schemas, data flow, deployment target, ownership, and license constraints.

Capability preflight: Confirm an OneFoundry plugin or connector packaging contract, installation path, credential handling, approval gates, and compatibility tests. External plugin scaffolds and personal marketplaces are host formats, not OneFoundry package formats; OneFoundry has no general plugin-creation action.

1. Define the smallest tool contract with typed inputs, outputs, errors, and side effects.
2. Assign credential storage, egress, approval, idempotency, and owner readback responsibilities.
3. Identify the packaging and upgrade contract that OneFoundry would need before implementation.
4. Return a design and setup sequence; stop before scaffolding files or registering a connector when the owner contract is absent.

Output: A plugin design brief or `blocked_for_package_contract`. This procedure does not create an external plugin, alter a marketplace, or grant a role new tools.
