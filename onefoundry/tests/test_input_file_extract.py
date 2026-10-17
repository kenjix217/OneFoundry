"""Untrusted input parsing produces bounded, labeled agent context."""

import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from onefoundry.input_file_extract import ExtractionError, _write_office_profile, extract_input


def _office_file(
    member: str, body: bytes, *, external: bool = False, extras: dict[str, bytes] | None = None
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr(member, body)
        for name, content in (extras or {}).items():
            archive.writestr(name, content)
        if external:
            archive.writestr(
                "word/_rels/document.xml.rels",
                b'<Relationships><Relationship Target="https://example.invalid" '
                b"TargetMode='External'/></Relationships>",
            )
    return output.getvalue()


def test_text_and_html_extract_without_scripts() -> None:
    plain = extract_input("notes.md", b"# District plan\nVisit schools", max_text_bytes=4096)
    assert plain.kind == "text" and plain.chunks[0].source_label == "notes.md: text 1"
    assert "Visit schools" in plain.chunks[0].content
    page = extract_input(
        "page.html", b"<h1>Budget</h1><script>steal()</script><p>Q1 spend</p>", max_text_bytes=4096
    )
    assert "Budget" in page.chunks[0].content and "Q1 spend" in page.chunks[0].content
    assert "steal" not in page.chunks[0].content


def test_office_text_and_external_reference_guard() -> None:
    body = b'<w:document xmlns:w="urn:word"><w:p><w:t>Sales targets</w:t></w:p></w:document>'
    document = _office_file("word/document.xml", body)
    result = extract_input("plan.docx", document, max_text_bytes=4096)
    assert result.kind == "text" and "Sales targets" in result.chunks[0].content
    with pytest.raises(ExtractionError, match="external"):
        extract_input(
            "plan.docx", _office_file("word/document.xml", body, external=True), max_text_bytes=4096
        )


def test_sheet_and_slide_sections_are_labeled() -> None:
    workbook = _office_file(
        "xl/workbook.xml",
        b"<workbook/>",
        extras={
            "xl/worksheets/sheet1.xml": (
                b'<worksheet><sheetData><row><c r="A1"><v>42</v></c></row></sheetData></worksheet>'
            ),
        },
    )
    sheet = extract_input("budget.xlsx", workbook, max_text_bytes=4096)
    assert sheet.chunks[0].source_label == "budget.xlsx: sheet 1 1"
    assert "A1=42" in sheet.chunks[0].content
    presentation = _office_file(
        "ppt/presentation.xml",
        b"<presentation/>",
        extras={
            "ppt/slides/slide1.xml": b'<p:sld xmlns:p="urn:p"><p:t>Launch phase</p:t></p:sld>',
        },
    )
    slide = extract_input("plan.pptx", presentation, max_text_bytes=4096)
    assert slide.chunks[0].source_label == "plan.pptx: slide 1 1"
    assert "Launch phase" in slide.chunks[0].content


def test_images_are_verified_without_claiming_text() -> None:
    output = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    png = output.getvalue()
    image = extract_input("photo.png", png, max_text_bytes=4096)
    assert image.kind == "visual" and image.chunks == ()
    with pytest.raises(ExtractionError, match="signature"):
        extract_input("photo.png", b"not a png", max_text_bytes=4096)
    with pytest.raises(ExtractionError, match="malformed"):
        extract_input("photo.png", png[:35], max_text_bytes=4096)


def test_image_ocr_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    monkeypatch.setattr(
        "onefoundry.input_file_extract._ocr_image", lambda _content: "db_password=secret-value"
    )
    with pytest.raises(ExtractionError, match="sensitive"):
        extract_input("photo.png", output.getvalue(), max_text_bytes=4096, require_image_ocr=True)


def _simple_pdf() -> bytes:
    stream = b"BT /F1 12 Tf 72 720 Td (Project roadmap) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    source = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(source))
        source.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(source)
    source.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        source.extend(f"{offset:010d} 00000 n \n".encode())
    source.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(source)


def test_pdf_text_has_page_label() -> None:
    result = extract_input("roadmap.pdf", _simple_pdf(), max_text_bytes=4096)
    assert result.kind == "text"
    assert result.chunks[0].source_label == "roadmap.pdf: page 1 1"
    assert "Project roadmap" in result.chunks[0].content


def test_legacy_converter_profile_disables_macros_and_active_content(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    _write_office_profile(profile)
    root = ET.parse(  # noqa: S314 - this test parses only the profile it just wrote.
        profile / "user" / "registrymodifications.xcu"
    ).getroot()
    namespace = "{http://openoffice.org/2001/registry}"
    properties = {prop.attrib[f"{namespace}name"]: prop.findtext("value") for prop in root.findall(".//prop")}
    assert properties["DisableMacrosExecution"] == "true"
    assert properties["DisableActiveContent"] == "true"
    assert properties["MacroSecurityLevel"] == "3"


def test_disallowed_or_sensitive_text_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="unsupported"):
        extract_input("archive.zip", b"PK\x03\x04", max_text_bytes=4096)
    with pytest.raises(ExtractionError, match="sensitive"):
        extract_input("notes.txt", b"db_password=very-secret-value", max_text_bytes=4096)
