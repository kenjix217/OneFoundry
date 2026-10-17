# current external-workflow contract map

**Trigger:** A developer asks whether a current external agent command or workflow exists in OneFoundry.

**Required inputs:** Named external action, actor, project/run context, expected side effect, and required result.

**Capability preflight:** No external API key, heartbeat, issue, case, routine, or workspace-control tool is admitted. Only Tool Gateway and Execution Gateway contracts documented in OneFoundry can be proposed.

**Procedure:** Classify the action as read, draft, approval-bound side effect, coding-provider execution, or unsupported host action. Map its owner and exact result/readback semantics; stop on unknown outcomes before retry.

**Output:** Return a capability mapping and safe next owner action. Never translate an external endpoint into an invented OneFoundry call.
