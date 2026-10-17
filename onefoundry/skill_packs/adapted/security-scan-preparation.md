# standard security scan preparation

Trigger: The operator requests a single-pass security audit of a repository or scoped directory.

Inputs: Exact target revision, requested scope, security policy, architecture context, and expected evidence and report format.

Capability preflight: Verify repository-wide source inventory, scoped file reads, scan identity, worker coordination, finding validation, coverage accounting, and report finalization. OneFoundry has bounded exact-file reads but no dedicated standard-scan service or authoritative coverage ledger.

1. Resolve the proposed target and the policy that should govern each component; flag missing or conflicting policy files.
2. Define assets, attacker entry points, trust boundaries, and high-value source paths for the scan request.
3. Specify finding evidence, validation, and coverage receipts that a real scan must return.
4. If the service is absent, identify the owner and prerequisites to run it; do not perform a partial text review under the scan label.

Output: A ready-to-review scan request or `blocked_for_scan_service` with exact gaps. No scan finding or complete coverage is asserted by this procedure.
