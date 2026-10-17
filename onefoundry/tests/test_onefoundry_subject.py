"""The pilot accepts organization subjects only."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.onefoundry_subject import SubjectRef


def test_organization_subject_is_typed() -> None:
    project_id, subject_id = uuid4(), uuid4()
    assert SubjectRef(project_id=project_id, subject_id=subject_id).subject_type == "organization"
    with pytest.raises(ValidationError):
        SubjectRef.model_validate(
            {"project_id": str(project_id), "subject_id": str(subject_id), "subject_type": "work_order"}
        )
