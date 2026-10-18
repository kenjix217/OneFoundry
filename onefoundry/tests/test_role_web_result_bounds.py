"""Fetch output must fit the live tool envelope after JSON encoding."""

import json

import pytest

from onefoundry.role_tool_admission import ToolOutputRejected
from onefoundry.role_web_admission import _checked_result
from onefoundry.role_web_tools import FETCH_TOOL_ID


def _fetch_result(text: str, *, url: str = "https://example.org/page") -> dict[str, object]:
    return {
        "url": url,
        "fetched_at": "2026-10-18T00:00:00+00:00",
        "title": "Example",
        "text": text,
        "text_truncated": False,
        "published_at": None,
    }


@pytest.mark.parametrize("text", ["x" * 16000, "漢😀" * 5000])
def test_fetch_excerpt_fits_serialized_output_bytes(text: str) -> None:
    original = _fetch_result(text)
    checked, encoded = _checked_result(original, FETCH_TOOL_ID, 16000)

    assert len(encoded) <= 16000
    assert checked["text_truncated"] is True
    assert isinstance(checked["text"], str)
    assert text.startswith(checked["text"])
    assert checked["url"] == original["url"]
    assert checked["title"] == original["title"]
    assert json.loads(encoded) == checked
    assert original["text"] == text
    assert original["text_truncated"] is False


def test_fetch_output_unchanged_when_it_fits() -> None:
    original = _fetch_result("A short page")
    checked, encoded = _checked_result(original, FETCH_TOOL_ID, 16000)
    assert checked == original
    assert json.loads(encoded) == original


def test_fetch_rejects_metadata_that_cannot_fit_even_with_empty_text() -> None:
    original = _fetch_result("Page", url="https://example.org/" + "a" * 1900)
    with pytest.raises(ToolOutputRejected, match="byte limit"):
        _checked_result(original, FETCH_TOOL_ID, 1024)
