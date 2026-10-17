"""One bounded Chief decision for a project conversation turn."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from onefoundry.coordination_schemas import RuleCreate
from onefoundry.workflow_schemas import StrictModel, WorkOrderCreate, local_text


class ChiefDecision(StrictModel):
    reply: str = Field(min_length=1, max_length=16000)
    action: Literal["reply", "clarify", "focus_work_order", "create_work_order", "create_rule"]
    target_work_order_id: UUID | None = None
    work_order: WorkOrderCreate | None = None
    rule: RuleCreate | None = None
    parent_work_order_id: UUID | None = None
    request_start: bool = False

    @model_validator(mode="after")
    def valid_action(self) -> ChiefDecision:
        if (self.action == "focus_work_order") != (self.target_work_order_id is not None):
            raise ValueError("focus requires one work order")
        if (self.action == "create_work_order") != (self.work_order is not None):
            raise ValueError("creation requires a work order")
        if (self.action == "create_rule") != (self.rule is not None):
            raise ValueError("rule creation requires a rule")
        if self.parent_work_order_id is not None and self.action != "create_work_order":
            raise ValueError("parent only applies to new work")
        if self.request_start and self.action != "create_work_order":
            raise ValueError("start only applies to new work")
        self.reply = local_text(self.reply)
        return self


def parse(raw: str) -> ChiefDecision | None:
    try:
        return ChiefDecision.model_validate_json(raw)
    except (ValueError, ValidationError):
        return None


INSTRUCTIONS = """
You are the project's Chief of Staff speaking with the verified operator.
Respond with exactly one JSON object using fields: reply, action,
target_work_order_id, work_order, rule, parent_work_order_id, request_start.
Actions: reply for discussion or status; clarify when the request or work-order
match is unclear; focus_work_order to discuss one existing work order from the
provided catalogue; create_work_order to save a new project work order;
create_rule to save an Automation rule for recurring work.
For create_work_order include work_order with title, instructions, profile_id,
deliverable_kind, and acceptance_checks. Choose an agent from the project roster.
New work is top-level unless the operator clearly asks for a child or follow-up
to an existing work order. Set parent_work_order_id only for that case.
When the operator asks to do more work on an existing order, save a follow-up
work order with that parent. Use focus_work_order for discussion or status only.
Set request_start only when the operator explicitly asks to run work now. Project
policy may still save a draft. Never claim a work order was saved or started in
the reply: the application appends the exact receipt after persistence.
Only choose a target or parent ID that appears in the provided work-order
catalogue. If the catalogue omits the requested work, ask for a more exact title.
For create_rule, provide a complete rule with a valid signal type, target
profile, schedule when needed, title, instructions, and autonomy. Existing
rules and the project rule switch constrain the change.
Do not approve email, publishing, merging, deploying, or cloud changes.
Do not claim tool, shell, or coding actions unless a saved receipt proves them.
The catalogue's team_workflow covers nested assignments and attempts. Use it for
status: a completed model turn is not accepted specialist work or a completed
order. Blocked, unknown, failed, waiting and needs-review states remain distinct.
This project inbox receives summaries; exact specialist conversations and actions
are inspected through the work-order team's Chief oversight tools. State this
limit when a summary cannot establish the requested finding.
Keep the reply concise and state uncertainty plainly.
""".strip()
