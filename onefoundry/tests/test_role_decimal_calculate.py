"""The calculation contract admits only small decimal strings and bounded results."""

from __future__ import annotations

import pytest

from onefoundry.role_decimal_calculate import CalculationError, calculate_decimal


@pytest.mark.parametrize(
    ("operation", "left", "right", "expected"),
    [
        ("add", "1.20", "2.30", "3.5"),
        ("subtract", "1", "2", "-1"),
        ("multiply", "-2.50", "0.20", "-0.5"),
        ("divide", "1", "8", "0.125"),
        ("divide", "1", "3", "0.333333333333"),
        ("add", "-0.00", "0", "0"),
    ],
)
def test_bounded_arithmetic_returns_canonical_decimal(
    operation: str, left: str, right: str, expected: str
) -> None:
    output = calculate_decimal({"operation": operation, "left": left, "right": right})
    assert output.model_dump() == {"result": expected}


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "power", "left": "2", "right": "3"},
        {"operation": "add", "left": 1.0, "right": "2"},
        {"operation": "add", "left": "1e3", "right": "2"},
        {"operation": "add", "left": "+1", "right": "2"},
        {"operation": "add", "left": "01", "right": "2"},
        {"operation": "add", "left": "NaN", "right": "2"},
        {"operation": "add", "left": "1000000000000", "right": "2"},
        {"operation": "add", "left": "0.0000000000001", "right": "2"},
        {"operation": "add", "left": "1", "right": "2", "secret": "token=confidential"},
        {"operation": "add", "left": "1"},
    ],
)
def test_malformed_arguments_are_rejected_without_echo(arguments: dict[str, object]) -> None:
    with pytest.raises(CalculationError) as error:
        calculate_decimal(arguments)
    assert error.value.code == "malformed_arguments"
    assert "confidential" not in str(error.value)


def test_division_by_zero_has_explicit_error() -> None:
    with pytest.raises(CalculationError) as error:
        calculate_decimal({"operation": "divide", "left": "1", "right": "0.00"})
    assert error.value.code == "divide_by_zero"


def test_result_overflow_has_explicit_error() -> None:
    with pytest.raises(CalculationError) as error:
        calculate_decimal({"operation": "divide", "left": "999999999999", "right": "0.000000000001"})
    assert error.value.code == "overflow"


def test_large_result_just_below_magnitude_bound_is_allowed() -> None:
    output = calculate_decimal(
        {"operation": "multiply", "left": "999999999999.999999999999", "right": "1000000"}
    )
    assert output.result == "999999999999999999.999999"


def test_nonzero_result_below_output_precision_is_rejected() -> None:
    with pytest.raises(CalculationError) as error:
        calculate_decimal({"operation": "multiply", "left": "0.000000000001", "right": "0.000000000001"})
    assert error.value.code == "underflow"


def test_rounding_is_deterministic_and_never_uses_binary_float() -> None:
    assert calculate_decimal({"operation": "add", "left": "0.1", "right": "0.2"}).result == "0.3"
    assert calculate_decimal({"operation": "divide", "left": "1", "right": "6"}).result == "0.166666666667"
