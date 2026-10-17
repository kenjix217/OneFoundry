"""Pure, bounded decimal arithmetic for an admitted role tool.

Inputs are JSON object fields containing plain decimal strings. Operands have at
most 12 integer and 12 fractional digits. Results have at most 18 integer and
12 fractional digits; division and other inexact results use half-even rounding.
This module grants no role access and records no events.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_OPERAND = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?\Z")
_RESULT = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?\Z")
_RESULT_LIMIT = Decimal("1000000000000000000")
_RESULT_QUANTUM = Decimal("0.000000000001")
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
        "left": {"type": "string", "minLength": 1, "maxLength": 26},
        "right": {"type": "string", "minLength": 1, "maxLength": 26},
    },
    "required": ["operation", "left", "right"],
    "additionalProperties": False,
}

CalculationErrorCode = Literal["malformed_arguments", "divide_by_zero", "overflow", "underflow"]


class CalculationError(ValueError):
    """A stable, non-reflecting calculation failure."""

    def __init__(self, code: CalculationErrorCode) -> None:
        self.code = code
        super().__init__(code)


class CalculateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation: Literal["add", "subtract", "multiply", "divide"]
    left: str
    right: str

    @field_validator("left", "right")
    @classmethod
    def bounded_decimal(cls, value: str) -> str:
        if len(value) > 26 or _OPERAND.fullmatch(value) is None:
            raise ValueError("operand must be a bounded plain decimal string")
        return value


class CalculateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    result: str


def _canonical(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    whole, separator, fraction = format(value, "f").partition(".")
    fraction = fraction.rstrip("0")
    return whole + (separator + fraction if fraction else "")


def calculate_decimal(arguments: object) -> CalculateResult:
    """Calculate a small exact-input expression with a bounded decimal result.

    Raises CalculationError with a code and no submitted content. The Tool
    Gateway remains responsible for admission, authorization, and receipts.
    """
    try:
        parsed = CalculateArguments.model_validate(arguments, strict=True)
    except ValidationError:
        raise CalculationError("malformed_arguments") from None

    left, right = Decimal(parsed.left), Decimal(parsed.right)
    if parsed.operation == "divide" and right.is_zero():
        raise CalculationError("divide_by_zero")

    try:
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            if parsed.operation == "add":
                raw = left + right
            elif parsed.operation == "subtract":
                raw = left - right
            elif parsed.operation == "multiply":
                raw = left * right
            else:
                raw = left / right
            if not raw.is_finite() or abs(raw) >= _RESULT_LIMIT:
                raise CalculationError("overflow")
            rounded = raw.quantize(_RESULT_QUANTUM)
            if abs(rounded) >= _RESULT_LIMIT:
                raise CalculationError("overflow")
    except DecimalException:
        raise CalculationError("overflow") from None

    if raw and not rounded:
        raise CalculationError("underflow")
    return CalculateResult(result=_canonical(rounded))


def parse_calculation(arguments: object) -> dict[str, str]:
    """Validate the whole arithmetic request before a tool intent is recorded."""
    calculate_decimal(arguments)
    return CalculateArguments.model_validate(arguments, strict=True).model_dump()


def validate_calculation(result: dict[str, Any]) -> None:
    if set(result) != {"result"} or not isinstance(result["result"], str):
        raise ValueError("invalid calculation result")
    value = result["result"]
    if not value or len(value) > 32 or _RESULT.fullmatch(value) is None:
        raise ValueError("calculation result is not a bounded decimal")
