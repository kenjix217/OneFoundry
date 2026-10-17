"""Assignment-specific result contracts shared by advertisement and validation."""

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from onefoundry.workflow_schemas import StrictModel


class _ReportBody(StrictModel):
    body: str = Field(min_length=1, max_length=12000)


class ReportArgs(_ReportBody):
    """Retain the existing wire model, including explicitly null optional fields."""

    artifact_id: UUID | None = None
    artifact_run_id: UUID | None = None
    verdict: Literal["passed", "needs_rework"] | None = None


class _ArtifactReport(_ReportBody):
    artifact_id: UUID
    artifact_run_id: UUID


class _QaReport(_ReportBody):
    verdict: Literal["passed", "needs_rework"]


_CORRECTIONS = {
    "text_report_body_only": (
        "Submit body only. Remove artifact_id, artifact_run_id and verdict; QA owns verdicts."
    ),
    "artifact_report_requires_saved_ids": (
        "Submit body, artifact_id and artifact_run_id from your own saved artifact. Remove verdict."
    ),
    "qa_report_requires_verdict": "Submit body and an independent QA verdict: passed or needs_rework.",
}


class ReportInputError(ValueError):
    """Known rejected input, with no private values or uncertain side effect."""

    def __init__(self, code: str) -> None:
        if code not in _CORRECTIONS:
            raise ValueError("unregistered report correction")
        self.code = code
        super().__init__(_CORRECTIONS[code])


def correction(reason: str) -> dict[str, str]:
    """The same safe response is available on original calls and receipt replay."""
    response = {"reason": reason}
    if reason in _CORRECTIONS:
        response["next_step"] = _CORRECTIONS[reason]
    return response


def specification(kind: str) -> dict[str, Any]:
    if kind == "text_report":
        schema = _ReportBody.model_json_schema()
        description = (
            "Save a confirmed text report with body only, and release declared dependent tasks. "
            "Do not supply artifact IDs or a verdict; QA owns review verdicts."
        )
    elif kind == "artifact":
        schema = _ArtifactReport.model_json_schema()
        description = (
            "Report your exact saved artifact with a short summary in body, artifact_id and "
            "artifact_run_id from the artifact tool receipt. Do not copy the artifact content into body: "
            "the Gateway reads and hashes those bytes. Do not supply a verdict."
        )
    elif kind == "qa_review":
        schema = _QaReport.model_json_schema()
        description = (
            "Independently review the confirmed dependency content and saved artifact, then report "
            "body and verdict: passed or needs_rework. The Gateway binds the review to those exact bytes."
        )
    else:
        raise ValueError("assignment has no admitted report contract")
    return {
        "type": "function",
        "name": "report_result",
        "description": description,
        "inputSchema": schema,
    }


def validate(kind: str, arguments: Mapping[str, Any]) -> ReportArgs:
    """Reject known incompatible fields before any report owner can write a result."""
    data = ReportArgs.model_validate(arguments)
    if kind == "text_report":
        if any(value is not None for value in (data.artifact_id, data.artifact_run_id, data.verdict)):
            raise ReportInputError("text_report_body_only")
    elif kind == "artifact":
        if data.artifact_id is None or data.artifact_run_id is None or data.verdict is not None:
            raise ReportInputError("artifact_report_requires_saved_ids")
    elif kind == "qa_review":
        if data.verdict is None:
            raise ReportInputError("qa_report_requires_verdict")
        # Older QA callers could include UUID references. The evidence owner
        # still derives and checks exact artifacts from the declared dependencies.
    else:
        raise ValueError("assignment has no admitted report contract")
    return data
