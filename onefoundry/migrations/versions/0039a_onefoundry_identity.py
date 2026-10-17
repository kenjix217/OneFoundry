"""Move the synthetic Sales recipient to the OneFoundry preview address.

The recipient is a fixed local .invalid address, not an outbound destination.
"""

import sqlalchemy as sa
from alembic import op

revision = "0039a_onefoundry_identity"
down_revision = "0039_onefoundry_intents"
branch_labels = None
depends_on = None

_OLD_RECIPIENT = "sales-preview@agentpod.invalid"
_NEW_RECIPIENT = "sales-preview@onefoundry.invalid"


def upgrade() -> None:
    op.drop_constraint("sales_drafts_recipient_fixed", "sales_drafts", type_="check")
    op.get_bind().execute(
        sa.text("UPDATE sales_drafts SET recipient = :new WHERE recipient = :old"),
        {"new": _NEW_RECIPIENT, "old": _OLD_RECIPIENT},
    )
    op.create_check_constraint(
        "sales_drafts_recipient_fixed",
        "sales_drafts",
        "recipient = 'sales-preview@onefoundry.invalid'",
    )


def downgrade() -> None:
    delivered = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM sales_drafts WHERE delivery_status = 'delivered')")
    )
    if delivered:
        raise RuntimeError("cannot downgrade: delivered OneFoundry previews must retain their recipient")
    op.drop_constraint("sales_drafts_recipient_fixed", "sales_drafts", type_="check")
    op.get_bind().execute(
        sa.text("UPDATE sales_drafts SET recipient = :old WHERE recipient = :new"),
        {"new": _NEW_RECIPIENT, "old": _OLD_RECIPIENT},
    )
    op.create_check_constraint(
        "sales_drafts_recipient_fixed",
        "sales_drafts",
        "recipient = 'sales-preview@agentpod.invalid'",
    )
