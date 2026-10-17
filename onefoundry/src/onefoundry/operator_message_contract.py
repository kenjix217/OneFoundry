"""Bounds and initial brief for operator chat messages."""

MAX_OPERATOR_MESSAGE_CHARS = 1_000_000
MAX_WORK_ORDER_INSTRUCTIONS_CHARS = 16_000
LONG_MESSAGE_BRIEF = (
    "Follow the operator's first saved message in this conversation. "
    "It contains the complete objective and must be read before acting."
)


def initial_work_order_instructions(body: str) -> str:
    return body if len(body) <= MAX_WORK_ORDER_INSTRUCTIONS_CHARS else LONG_MESSAGE_BRIEF
