# Company package planning

**Trigger:** An operator wants to translate a company or repository concept into an OneFoundry setup proposal.

**Required inputs:** Company goal, proposed projects, role responsibilities, source repository description, and operator constraints.

**Capability preflight:** OneFoundry has project/profile editors, but a role run has no company-package generator or external company package owner. `create_draft_work_order_v1` can prepare only a same-project draft when admitted.

**Procedure:** Extract the minimum project, starting profiles, first work order, QA handoff, and unresolved policy decisions. Check role tools and memory scopes independently of job titles. Draft a staged setup sequence the operator can review.

**Output:** Return a project/profile/work-order proposal and missing decisions. Mark actual project creation, profile creation, and package export as operator actions.
