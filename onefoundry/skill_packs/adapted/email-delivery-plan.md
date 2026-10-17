# Email task and delivery plan

**Trigger:** An operator wants an agent to read, draft, or send email.

**Required inputs:** Mailbox owner, recipients, purpose, approved content, sensitivity, and delivery requirement.

**Capability preflight:** OneFoundry has no AgentMail inbox or email connector. `write_text_artifact_v1` can save a draft; sending requires explicit approval and future delivery readback.

**Procedure:** Separate inbound request evidence from proposed reply, draft exact text, verify recipient and attachments with the operator, and define how sent/delivered/unknown will be distinguished.

**Output:** Return a draft and delivery checklist. Mark inbox read and send blocked; never claim delivery from draft creation.
