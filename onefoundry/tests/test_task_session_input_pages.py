"""Paged team reads preserve original text bytes and bound transformed text."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.input_file_bytes import InputFileBytes
from onefoundry.input_file_context import ContextSelection
from onefoundry.task_session_dependency_context import TeamContextBudgetExceeded
from onefoundry.task_session_file_context import append_files
from onefoundry.task_session_input_pages import InputFilePageArgs, read_input_file_page


def _connection(rows: list[dict[str, object]], chunks: list[dict[str, object]] | None = None) -> Mock:
    connection = Mock()

    def execute(statement: object, _params: object) -> Mock:
        result = Mock()
        result.mappings.return_value.all.return_value = (
            chunks if "FROM input_file_chunks" in str(statement) else rows
        )
        return result

    connection.execute.side_effect = execute
    return connection


def _saved(tmp_path: Path, *, extension: str, content: bytes) -> tuple[Path, UUID, UUID, dict[str, object]]:
    root = tmp_path / "files"
    root.mkdir(mode=0o700, parents=True)
    project, file_id = uuid4(), uuid4()
    saved = InputFileBytes(root).write(project, file_id, [content], max_bytes=len(content))
    row: dict[str, object] = {
        "id": file_id,
        "display_name": f"reference.{extension}",
        "extension": extension,
        "sha256": saved.sha256,
        "byte_size": saved.byte_size,
        "status": "ready_text",
    }
    return root, project, file_id, row


def test_original_utf8_pages_reconstruct_exact_bytes_including_final_newline(tmp_path: Path) -> None:
    original = ("source café 中文\n" * 2000).encode() + b"final\n"
    root, project, file_id, row = _saved(tmp_path, extension="txt", content=original)
    connection = _connection([row])
    parts: list[str] = []
    offset = 0
    while True:
        page = read_input_file_page(
            connection, str(root), project, uuid4(), InputFilePageArgs(file_id=file_id, offset=offset), 1024
        )
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 1024
        assert page["representation"] == "original_utf8"
        assert page["representation_sha256"] == page["file_sha256"] == hashlib.sha256(original).hexdigest()
        parts.append(page["content"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(parts).encode() == original
    end = read_input_file_page(
        connection,
        str(root),
        project,
        uuid4(),
        InputFilePageArgs(file_id=file_id, offset=len(original.decode())),
        1024,
    )
    assert end["content"] == "" and end["next_offset"] is None


def test_extracted_representation_has_its_own_digest_and_verified_sections(tmp_path: Path) -> None:
    root, project, file_id, row = _saved(tmp_path, extension="md", content=b"  original\n")
    chunk = {
        "ordinal": 0,
        "source_label": "part one",
        "content": "original",
        "sha256": hashlib.sha256(b"original").hexdigest(),
    }
    connection = _connection([row], [chunk])
    page = read_input_file_page(
        connection, str(root), project, uuid4(), InputFilePageArgs(file_id=file_id), 1024
    )
    assert page["representation"] == "extracted_sections"
    assert page["content"] == "Section: part one\noriginal"
    assert page["representation_sha256"] == hashlib.sha256(page["content"].encode()).hexdigest()
    assert page["representation_sha256"] != page["file_sha256"]
    chunk["sha256"] = "0" * 64
    with pytest.raises(Conflict, match="integrity"):
        read_input_file_page(
            connection, str(root), project, uuid4(), InputFilePageArgs(file_id=file_id), 1024
        )


def test_bounded_index_and_sensitive_labels_do_not_disclose_names(tmp_path: Path) -> None:
    root, project, file_id, row = _saved(tmp_path, extension="md", content=b"original")
    row["display_name"] = "token=secret_value_12345.md"
    rows = [row]
    for _ in range(8):
        rows.append({**row, "id": uuid4(), "display_name": "another-file.md"})
    connection = _connection(rows)
    collected: list[dict[str, object]] = []
    offset = 0
    while True:
        page = read_input_file_page(
            connection, str(root), project, uuid4(), InputFilePageArgs(offset=offset), 400
        )
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 400
        collected.extend(page["files"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(collected) == 9
    assert collected[0]["display_name"] == "[sensitive filename]"
    assert "secret_value_12345" not in json.dumps(collected)
    chunk = {
        "ordinal": 0,
        "source_label": "token=secret_value_12345: text 1",
        "content": "original",
        "sha256": hashlib.sha256(b"original").hexdigest(),
    }
    page = read_input_file_page(
        _connection([row], [chunk]), str(root), project, uuid4(), InputFilePageArgs(file_id=file_id), 1024
    )
    assert page["display_name"] == "[sensitive filename]"
    assert page["content"] == "Section: 1\noriginal"
    assert "secret_value_12345" not in json.dumps(page)


def test_unready_missing_corrupt_sensitive_and_invalid_pages_fail_closed(tmp_path: Path) -> None:
    root, project, file_id, row = _saved(tmp_path, extension="txt", content=b"ordinary evidence\n")
    args = InputFilePageArgs(file_id=file_id)
    with pytest.raises(NotFound):
        read_input_file_page(_connection([]), str(root), project, uuid4(), args, 1024)
    row["status"] = "pending"
    with pytest.raises(NotFound):
        read_input_file_page(_connection([row]), str(root), project, uuid4(), args, 1024)
    row["status"] = "rejected"
    with pytest.raises(NotFound):
        read_input_file_page(_connection([row]), str(root), project, uuid4(), args, 1024)
    row["status"] = "ready_text"
    row["sha256"] = "0" * 64
    with pytest.raises(Conflict, match="storage integrity"):
        read_input_file_page(_connection([row]), str(root), project, uuid4(), args, 1024)
    row["sha256"] = hashlib.sha256(b"ordinary evidence\n").hexdigest()
    with pytest.raises(ValueError, match="offset exceeds"):
        read_input_file_page(
            _connection([row]),
            str(root),
            project,
            uuid4(),
            InputFilePageArgs(file_id=file_id, offset=100),
            1024,
        )
    with pytest.raises(TeamContextBudgetExceeded):
        read_input_file_page(_connection([row]), str(root), project, uuid4(), args, 10)
    for invalid in ({"file_id": "bad"}, {"file_id": file_id, "offset": -1}, {"file_id": file_id, "extra": 1}):
        with pytest.raises(ValidationError):
            InputFilePageArgs.model_validate(invalid)

    sensitive = b"token=secret_value_12345\n"
    secret_root, secret_project, secret_file, secret_row = _saved(
        tmp_path / "sensitive", extension="txt", content=sensitive
    )
    with pytest.raises(Conflict, match="sensitive"):
        read_input_file_page(
            _connection([secret_row]),
            str(secret_root),
            secret_project,
            uuid4(),
            InputFilePageArgs(file_id=secret_file),
            1024,
        )


def test_unlimited_team_context_keeps_paged_file_reference_after_large_required_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, root_id, order_id, attempt_id, file_id = (uuid4() for _ in range(5))
    gateway, settings, connection = Mock(), Mock(), Mock()
    settings.onefoundry_max_context_bytes = None
    settings.live_text_max_prompt_bytes = 32000
    settings.input_file_max_link_count = 8
    file_row = {"id": file_id, "display_name": "reference.txt", "sha256": "a" * 64, "status": "ready_text"}
    monkeypatch.setattr(
        "onefoundry.task_session_file_context.scoped_text_files",
        lambda *_args: [file_row],
    )
    selected_budgets: list[int] = []

    def select_stub(*args: object, **kwargs: object) -> ContextSelection:
        assert isinstance(args[5], int)
        selected_budgets.append(args[5])
        return ContextSelection("", (), 1, ())

    monkeypatch.setattr("onefoundry.task_session_file_context.select_context", select_stub)
    connection.scalar.return_value = None
    parts = ["x" * 33000]
    append_files(
        gateway,
        settings,
        connection,
        project_id,
        {
            "root_work_order_id": root_id,
            "work_order_id": order_id,
            "id": attempt_id,
            "root_instructions": "Read the attached source.",
            "prompt_sha256": None,
        },
        parts,
    )
    assert len(parts) == 2
    assert "onefoundry.read_input_file" in parts[1]
    assert selected_budgets == [0]
    gateway._event.assert_called_once()

    settings.onefoundry_max_context_bytes = 32000
    with pytest.raises(TeamContextBudgetExceeded):
        append_files(
            gateway,
            settings,
            connection,
            project_id,
            {
                "root_work_order_id": root_id,
                "work_order_id": order_id,
                "id": attempt_id,
                "root_instructions": "Read the attached source.",
                "prompt_sha256": None,
            },
            ["x" * 33000],
        )
