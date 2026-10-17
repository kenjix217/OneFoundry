# Private security advisory response plan

**Trigger:** An operator supplies a confidential vulnerability report and asks for a response sequence.

**Required inputs:** Approved non-secret summary, affected component/version, exploit evidence, reporter contact owner, and disclosure constraints.

**Capability preflight:** Do not send secrets or private advisory bodies to an AI provider. OneFoundry has no advisory, private-fork, CVE, or release connector; outbound contact and publication need approval.

**Procedure:** From sanitized facts, separate containment, reproduction, fix validation, reporter coordination, release, and public disclosure. Name the owner and evidence gate for each stage; flag any unknown impact.

**Output:** Return a confidential action plan and `blocked` for missing sanitized input or owner authorization. Do not acknowledge, publish, or assign a CVE.
