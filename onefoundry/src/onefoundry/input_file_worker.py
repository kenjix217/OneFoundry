"""Restricted local document worker; never receives model credentials."""

from __future__ import annotations

import hashlib
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, create_engine, text

from onefoundry.input_file_bytes import InputFileBytes, InputFileInvalid
from onefoundry.input_file_extract import EXTRACTOR_VERSION, LEGACY_EXTENSIONS, ExtractionError, extract_input
from onefoundry.settings import Settings


def _rejection_code(error: InputFileInvalid | ExtractionError) -> str:
    if isinstance(error, InputFileInvalid):
        return "integrity_failure"
    detail = str(error).lower()
    for word, code in (
        ("sensitive", "sensitive_content"),
        ("signature", "format_mismatch"),
        ("encrypted", "encrypted_or_malformed"),
        ("exceed", "limit_exceeded"),
        ("timed out", "processing_timeout"),
        ("unavailable", "extractor_unavailable"),
        ("no extractable", "no_extractable_text"),
        ("unsupported", "unsupported_content"),
        ("malformed", "malformed_file"),
    ):
        if word in detail:
            return code
    return "extraction_rejected"


def process_one(engine: Engine, store: InputFileBytes, settings: Settings) -> bool:
    """Claim one pending original and record its exact parse outcome."""
    rejected: tuple[Any, Any] | None = None
    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id,project_id,display_name,extension,sha256,byte_size FROM input_files "
                    "WHERE status='pending' ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        project_id, file_id = row["project_id"], row["id"]
        try:
            original = store.read(project_id, file_id, row["sha256"], row["byte_size"])
            extracted = extract_input(
                row["display_name"],
                original,
                max_text_bytes=settings.input_file_max_text_bytes,
                require_image_ocr=True,
            )
            for ordinal, chunk in enumerate(extracted.chunks):
                connection.execute(
                    text(
                        "INSERT INTO input_file_chunks "
                        "(id,project_id,actor_id,file_id,ordinal,source_label,content,sha256) VALUES "
                        "(:id,:project_id,:actor_id,:file_id,:ordinal,:label,:content,:sha256)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": project_id,
                        "actor_id": settings.operator_actor_id,
                        "file_id": file_id,
                        "ordinal": ordinal,
                        "label": chunk.source_label,
                        "content": chunk.content,
                        "sha256": hashlib.sha256(chunk.content.encode()).hexdigest(),
                    },
                )
            connection.execute(
                text(
                    "UPDATE input_files SET status=:status,"
                    "mime_type=CASE WHEN :canonical THEN :mime_type ELSE mime_type END,"
                    "extractor_version=:version,finished_at=now() "
                    "WHERE project_id=:project_id AND id=:file_id AND status='pending'"
                ),
                {
                    "status": "ready_text" if extracted.kind == "text" else "ready_visual",
                    "mime_type": extracted.mime_type,
                    "canonical": row["extension"] not in LEGACY_EXTENSIONS,
                    "version": EXTRACTOR_VERSION,
                    "project_id": project_id,
                    "file_id": file_id,
                },
            )
        except (InputFileInvalid, ExtractionError) as exc:
            reason = _rejection_code(exc)
            connection.execute(
                text(
                    "UPDATE input_files SET status='rejected',reason_code=:reason,finished_at=now() "
                    "WHERE project_id=:project_id AND id=:file_id AND status='pending'"
                ),
                {"reason": reason, "project_id": project_id, "file_id": file_id},
            )
            rejected = project_id, file_id
    if rejected is not None:
        try:
            store.remove(*rejected)
        except InputFileInvalid:
            pass  # The database rejection remains authoritative; an orphan sweep can remove it.
    return True


def main() -> None:
    settings = Settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    store = InputFileBytes(settings.input_file_root)
    while True:
        if not process_one(engine, store, settings):
            time.sleep(2)


if __name__ == "__main__":
    main()
