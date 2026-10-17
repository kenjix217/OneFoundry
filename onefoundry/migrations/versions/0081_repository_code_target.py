"""Admit exact approved maintenance targets without relaxing prior fixture contracts."""

import sqlalchemy as sa
from alembic import op

revision = "0081_repository_code_target"
down_revision = "0080_chief_conversation_trash"
branch_labels = None
depends_on = None

CONSTRAINTS = {
    "external_code_fixture_valid": "fixture_id='repository_code_v1'",
    "external_code_adapter_valid": "fixture_id='repository_code_v1' AND adapter_id IN ('codex_cli','claude"
    "_code')",
    "external_code_source_snapshot_valid": "fixture_id='repository_code_v1' AND source_id IS NOT NULL "
    "AND source_root_id='workspace' AND source_relative_path IS NOT NULL "
    "AND source_branch IS NOT NULL AND source_revision ~ '^[0-9a-f]{40}$' "
    "AND idempotency_key IS NOT NULL AND intent_sha256 ~ '^[0-9a-f]{64}$' "
    "AND snapshot_attempt_id IS NULL AND snapshot_source_id IS NULL "
    "AND snapshot_account_id IS NULL AND snapshot_owner IS NULL AND snapshot_repo IS NULL "
    "AND snapshot_branch IS NULL AND snapshot_revision IS NULL AND snapshot_tree IS NULL "
    "AND snapshot_path IS NULL AND snapshot_sha256 IS NULL AND snapshot_byte_size IS NULL "
    "AND snapshot_request_sha256 IS NULL AND snapshot_receipt_sha256 IS NULL",
}


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conrelid='external_code_attempts'::regclass "
            "AND conname='external_code_repository_retired'"
        )
    ):
        op.drop_constraint("external_code_repository_retired", "external_code_attempts", type_="check")
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conrelid='task_session_tool_calls'::regclass "
            "AND conname='task_session_extension_retired'"
        )
    ):
        op.drop_constraint("task_session_extension_retired", "task_session_tool_calls", type_="check")
    for name, clause in CONSTRAINTS.items():
        original = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid="
                "'external_code_attempts'::regclass AND conname=:name"
            ),
            {"name": name},
        )
        if not isinstance(original, str) or not original.startswith("CHECK ("):
            raise RuntimeError("external coding constraint is unavailable")
        op.drop_constraint(name, "external_code_attempts", type_="check")
        op.create_check_constraint(name, "external_code_attempts", f"({original[7:-1]}) OR ({clause})")
    original = connection.scalar(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid="
            "'task_session_tool_calls'::regclass AND conname='task_session_tool_calls_name_valid'"
        )
    )
    if not isinstance(original, str) or not original.startswith("CHECK ("):
        raise RuntimeError("native tool name contract unavailable")
    op.drop_constraint("task_session_tool_calls_name_valid", "task_session_tool_calls", type_="check")
    op.create_check_constraint(
        "task_session_tool_calls_name_valid",
        "task_session_tool_calls",
        f"({original[7:-1]}) OR tool_name IN ('onefoundry.propose_maintenance_target',"
        "'onefoundry.inspect_capability',"
        "'onefoundry.read_extension_status','onefoundry.request_extension',"
        "'onefoundry.build_extension','onefoundry.finish_extension_build','onefoundry.verify_extension_candidate',"
        "'onefoundry.finish_extension_review','onefoundry.propose_extension_activation','onefoundry.propose_company_knowledge')",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM task_session_tool_calls WHERE tool_name IN "
            "('onefoundry.propose_maintenance_target','onefoundry.inspect_capability','onefoundry.read_extension_status','onefoundry.request_extension','onefoundry.build_extension',"
            "'onefoundry.finish_extension_build','onefoundry.verify_extension_candidate','onefoundry.finish_extension_review',"
            "'onefoundry.propose_extension_activation','onefoundry.propose_company_knowledge'))"
        )
    ):
        raise RuntimeError("Extension tool receipts exist; restore a checkpoint to downgrade")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM external_code_attempts WHERE fixture_id='repository_code_v1')")
    ):
        raise RuntimeError("Maintenance attempts exist; restore a checkpoint to downgrade")
    # Preserve all historic contracts. A downgrade rejects the new selector.
    op.create_check_constraint(
        "external_code_repository_retired", "external_code_attempts", "fixture_id<>'repository_code_v1'"
    )
    op.create_check_constraint(
        "task_session_extension_retired",
        "task_session_tool_calls",
        "tool_name NOT IN ('onefoundry.propose_maintenance_target','onefoundry.inspect_capability',"
        "'onefoundry.read_extension_status',"
        "'onefoundry.request_extension','onefoundry.build_extension','onefoundry.finish_extension_build',"
        "'onefoundry.verify_extension_candidate','onefoundry.finish_extension_review',"
        "'onefoundry.propose_extension_activation','onefoundry.propose_company_knowledge')",
    )
