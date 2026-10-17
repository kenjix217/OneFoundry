"""Local, deterministic DreamCycle and bounded lexical memory policy.

Adapted from historical KJ31 candidate sanitation and memory packet shaping.
The old storage, vector,
provider, and artifact authorization contracts are intentionally not copied.
"""

import re
import unicodedata
from uuid import NAMESPACE_URL, UUID, uuid5

from onefoundry.workflow_schemas import local_text

MAX_SUMMARY_BYTES = 4096
MAX_QUERY_BYTES = 120
_RAW_TOOL_RE = re.compile(r"(\[TOOL_REQUEST:|</?tool_call>|TOOL_REQUEST:\s*[A-Za-z0-9_]+\|)", re.I)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_WORDS_RE = re.compile(r"[\w]+", re.UNICODE)


def candidate_summary(value: str) -> str:
    """Bound operator text before it can become a reviewable candidate."""
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\n\t" for char in value):
        raise ValueError("text contains unsupported control characters")
    if _RAW_TOOL_RE.search(value):
        raise ValueError("text contains a tool request marker")
    summary = _MULTISPACE_RE.sub(" ", value.strip())
    summary = re.sub(r"\n{3,}", "\n\n", summary)
    summary = local_text(summary)
    if len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise ValueError("summary exceeds byte limit")
    return summary


def candidate_id(result_id: UUID, summary: str) -> UUID:
    """Stable proposal identity for one immutable, source-linked result."""
    # Keep this historical namespace so existing proposals retain their IDs.
    return uuid5(NAMESPACE_URL, f"agentpod.operator_result.v1\x00{result_id}\x00{summary}")


def query_terms(query: str) -> tuple[str, ...]:
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("query exceeds byte limit")
    return tuple(dict.fromkeys(term.casefold() for term in _WORDS_RE.findall(query)))[:10]


def work_memory_query(title: str, instructions: str) -> str:
    """Bound work-order words used for local source selection."""
    words = dict.fromkeys(_WORDS_RE.findall(f"{title} {instructions}"))
    chosen: list[str] = []
    for word in words:
        candidate = " ".join([*chosen, word])
        if len(candidate.encode("utf-8")) > MAX_QUERY_BYTES or len(chosen) >= 10:
            break
        chosen.append(word)
    return " ".join(chosen)
