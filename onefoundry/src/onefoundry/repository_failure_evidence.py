"""Failure artifacts are repair evidence, never permission to release a build."""

from onefoundry.external_code_schemas import ExternalCodeReceipt


def validate_failed_checks(receipt: ExternalCodeReceipt) -> None:
    before = [test for test in receipt.tests if test.name.startswith("baseline_")]
    after = [test for test in receipt.tests if test.name.startswith("candidate_")]
    if (
        not before
        or len(before) != len(after)
        or not any(test.status == "failed" for test in before)
        or not any(test.status == "failed" for test in after)
        or receipt.provider_session_id is None
        or any((test.status == "passed") != (test.exit_code == 0) for test in receipt.tests)
    ):
        raise ValueError("failed maintenance patch lacks completed failing checks")
