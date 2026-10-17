"""Capture accepts only explicit, bounded, public company conversation content."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.company_knowledge_proposal import KnowledgeCitation
from onefoundry.company_memory_schemas import CaptureCreate


def capture(**changes: object) -> CaptureCreate:
    data: dict[str, object] = {
        "client_capture_id": uuid4(),
        "source_ref": "operator-provided meeting excerpt",
        "authorized_company_content": True,
        "messages": [{"speaker": "user", "text": "Copper is our project name", "sent_at": datetime.now(UTC)}],
        "summary": "Copper is the project name",
        "scope": "project",
    }
    return CaptureCreate.model_validate({**data, **changes})


def test_capture_requires_explicit_authorization_and_visible_roles() -> None:
    assert capture().messages[0].speaker == "user"
    invalid: tuple[dict[str, object], ...] = (
        {"authorized_company_content": False},
        {"messages": []},
        {"messages": [{"speaker": "system", "text": "Private instructions", "sent_at": datetime.now(UTC)}]},
        {"summary": "password=synthetic-credential"},
        {"source_ref": "postgresql://user:credential@host/db"},
        {"messages": [{"speaker": "user", "text": "Private", "sent_at": datetime.now()}]},
    )
    for changes in invalid:
        with pytest.raises(ValidationError):
            capture(**changes)


def test_capture_byte_bound_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        capture(messages=[{"speaker": "assistant", "text": "界" * 6000, "sent_at": datetime.now(UTC)}])
    with pytest.raises(ValidationError):
        capture(assistant_internal_memory="must never export")


def citation(excerpt: str) -> KnowledgeCitation:
    return KnowledgeCitation(
        source_id=uuid4(), revision="a" * 40, path="catalog.go", sha256="b" * 64, excerpt=excerpt
    )


@pytest.mark.parametrize("excerpt", ['\tPublicAI     ModuleKey = "public_ai"', "\n\n\n    return a-b\n"])
def test_repository_citation_preserves_verbatim_source_whitespace(excerpt: str) -> None:
    assert citation(excerpt).excerpt == excerpt


@pytest.mark.parametrize(
    "excerpt",
    [
        "",
        "  \t\n",
        "text\x00",
        "text\u200b",
        "[TOOL_REQUEST:danger]",
        "password=synthetic-credential",
        "界" * 1500,
    ],
)
def test_repository_citation_retains_content_safety_bounds(excerpt: str) -> None:
    with pytest.raises(ValidationError):
        citation(excerpt)
