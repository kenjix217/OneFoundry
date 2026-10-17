"""Keep a completed provider observation when its disposable candidate is rejected."""

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.runner_spool import REASON, SESSION_ID
from runner.external_cli import CliResult
from runner.local_source_stage import StageError


class CandidateRejected(StageError):
    def __init__(self, reason: str, observation: CliResult) -> None:
        self.observation = validated(observation)
        super().__init__(self.observation.reason_code if self.observation is not observation else reason)


def validated(observation: CliResult) -> CliResult:
    if (
        observation.provider_session_id is not None
        and (
            SESSION_ID.fullmatch(observation.provider_session_id) is None
            or contains_sensitive_text(observation.provider_session_id)
        )
    ) or REASON.fullmatch(observation.reason_code) is None:
        return CliResult("unknown", "maintenance_provider_observation_invalid", None, None, None, b"")
    return observation


def copy_failure(error: OSError | ValueError, observation: CliResult) -> CandidateRejected:
    known = {
        "maintenance_candidate_outside_scope",
        "maintenance_candidate_protected_content",
        "maintenance_source_symlink_directory",
        "maintenance_source_invalid_file",
        "maintenance_source_inventory_limit",
    }
    reason = (
        str(error)
        if isinstance(error, ValueError) and str(error) in known
        else ("maintenance_candidate_copy_failed")
    )
    return CandidateRejected(reason, observation)
