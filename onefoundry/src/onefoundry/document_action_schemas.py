"""Exact folder operations; cell addresses cannot select names or whole sheets."""

import math
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator

from onefoundry.workflow_schemas import StrictModel, local_text

Cell = str | int | float | bool


def dimensions(address: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6}):([A-Z]{1,3})([1-9][0-9]{0,6})", address)
    if match is None:
        raise ValueError("use a bounded A1 rectangle, for example A1:B2")

    def column(value: str) -> int:
        result = 0
        for char in value:
            result = result * 26 + ord(char) - ord("A") + 1
        return result

    left, top, right, bottom = match.groups()
    rows, columns = int(bottom) - int(top) + 1, column(right) - column(left) + 1
    if rows < 1 or columns < 1 or int(bottom) > 1048576 or column(right) > 16384:
        raise ValueError("range is reversed or outside the worksheet")
    return rows, columns


class FileTarget(StrictModel):
    source_id: UUID
    file_id: str = Field(min_length=1, max_length=200)

    @field_validator("file_id")
    @classmethod
    def file_identity(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_!.-]+", value) is None or value in {".", ".."}:
            raise ValueError("invalid provider file identity")
        return value


class RangeRead(FileTarget):
    sheet: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=5, max_length=30)

    @field_validator("sheet")
    @classmethod
    def sheet_name(cls, value: str) -> str:
        return local_text(value)

    @field_validator("address")
    @classmethod
    def rectangle(cls, value: str) -> str:
        dimensions(value)
        return value


class Search(StrictModel):
    source_id: UUID
    query: str = Field(min_length=1, max_length=100)

    @field_validator("query")
    @classmethod
    def search_text(cls, value: str) -> str:
        return local_text(value)


class ReplaceFile(FileTarget):
    operation: Literal["replace_file"]
    input_file_id: UUID


class EditCells(RangeRead):
    operation: Literal["edit_cells"]
    values: list[list[Cell]] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def literal_cells(cls, values: list[list[Cell]]) -> list[list[Cell]]:
        for row in values:
            for cell in row:
                if isinstance(cell, float) and not math.isfinite(cell):
                    raise ValueError("cell numbers must be finite")
                if isinstance(cell, str):
                    local_text(cell, allow_empty=True)
                    if cell.lstrip().startswith(("=", "+", "-", "@")):
                        raise ValueError("formula-like cell text is not supported")
        return values


class ShareFile(FileTarget):
    operation: Literal["share_file"]
    email: str = Field(min_length=3, max_length=254)
    access: Literal["read", "write"]

    @field_validator("email")
    @classmethod
    def recipient(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value) is None:
            raise ValueError("use one plain email address")
        return local_text(value).lower()


Action = Annotated[ReplaceFile | EditCells | ShareFile, Field(discriminator="operation")]
ACTION: TypeAdapter[ReplaceFile | EditCells | ShareFile] = TypeAdapter(Action)


class ActionInput(FileTarget):
    """Object-shaped function schema; null unused fields are removed before union validation."""

    operation: Literal["replace_file", "edit_cells", "share_file"]
    input_file_id: UUID | None = None
    sheet: str | None = None
    address: str | None = None
    values: list[list[Cell]] | None = None
    email: str | None = None
    access: Literal["read", "write"] | None = None


def parse_action(raw: object) -> ReplaceFile | EditCells | ShareFile:
    flat = ActionInput.model_validate(raw).model_dump(mode="json", exclude_none=True)
    return ACTION.validate_python(flat)
