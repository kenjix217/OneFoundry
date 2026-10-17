"""Image priority and optional text budget use exact work-order ownership."""

from uuid import UUID, uuid4

from onefoundry.input_file_context import select_context


class Rows:
    def __init__(self, values: list[dict[str, object]]) -> None:
        self.values = values

    def mappings(self) -> "Rows":
        return self

    def all(self) -> list[dict[str, object]]:
        return self.values


class Connection:
    def __init__(self, files: list[dict[str, object]], chunks: list[dict[str, object]]) -> None:
        self.files = files
        self.chunks = chunks

    def execute(self, statement: object, _params: object) -> Rows:
        return Rows(self.files if "FROM input_files" in str(statement) else self.chunks)


def _visual(file_id: UUID, scope: str) -> dict[str, object]:
    return {
        "id": file_id,
        "display_name": f"{scope}.png",
        "sha256": "a" * 64,
        "byte_size": 100,
        "mime_type": "image/png",
        "status": "ready_visual",
        "reason_code": None,
        "scope": scope,
    }


def test_mandatory_image_has_priority_over_optional_project_image() -> None:
    project_image = UUID(int=1)
    order_image = UUID(int=2)
    chosen = select_context(
        Connection([_visual(project_image, "project"), _visual(order_image, "work_order")], []),
        uuid4(),
        uuid4(),
        [],
        "inspect",
        2048,
        8,
        allow_visual=True,
        max_visual_bytes=100,
        max_visual_count=1,
    )
    assert [item.file_id for item in chosen.visuals] == [order_image]


def test_project_image_requires_explicit_selection() -> None:
    project_image = uuid4()
    connection = Connection([_visual(project_image, "project")], [])
    automatic = select_context(
        connection,
        uuid4(),
        uuid4(),
        [],
        "inspect",
        2048,
        8,
        allow_visual=True,
        max_visual_bytes=100,
        max_visual_count=1,
    )
    assert automatic.visuals == ()
    selected = select_context(
        connection,
        uuid4(),
        uuid4(),
        [project_image],
        "inspect",
        2048,
        8,
        allow_visual=True,
        max_visual_bytes=100,
        max_visual_count=1,
    )
    assert [item.file_id for item in selected.visuals] == [project_image]


def test_optional_project_section_can_be_omitted_when_budget_is_exhausted() -> None:
    file_id = uuid4()
    content = "A very long reference section"
    import hashlib

    chosen = select_context(
        Connection(
            [
                {
                    "id": file_id,
                    "display_name": "context.md",
                    "sha256": "a" * 64,
                    "byte_size": len(content),
                    "mime_type": "text/markdown",
                    "status": "ready_text",
                    "reason_code": None,
                    "scope": "project",
                },
            ],
            [
                {
                    "id": uuid4(),
                    "file_id": file_id,
                    "ordinal": 0,
                    "source_label": "context.md: text 1",
                    "content": content,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "rank": 0,
                },
            ],
        ),
        uuid4(),
        uuid4(),
        [],
        "inspect",
        1,
        8,
    )
    assert chosen.text == ""
    assert chosen.omitted_chunks == 1
