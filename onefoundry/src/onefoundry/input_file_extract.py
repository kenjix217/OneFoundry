"""Bounded text extraction from untrusted operator input files.

Call only from the restricted input-file worker. The parser never interprets
document instructions as OneFoundry policy or makes an outbound request.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from onefoundry.role_sensitive_text import contains_sensitive_text

TEXT_EXTENSIONS = {"txt", "md", "csv", "tsv", "json", "yaml", "yml", "xml", "html", "htm"}
OFFICE_PART = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
LEGACY_EXTENSIONS = {"doc", "xls", "ppt", "rtf", "odt", "ods", "odp"}
ALL_EXTENSIONS = TEXT_EXTENSIONS | set(OFFICE_PART) | IMAGE_EXTENSIONS | LEGACY_EXTENSIONS | {"pdf"}
EXTRACTOR_VERSION = "input-v1"
MAX_CHUNK_CHARS = 1800
MAX_ZIP_MEMBERS = 500
MAX_ZIP_UNCOMPRESSED = 32 * 1024 * 1024
MAX_OFFICE_XML_MEMBER = 8 * 1024 * 1024


class ExtractionError(ValueError):
    """The file has no safe representation under the configured limits."""


@dataclass(frozen=True, slots=True)
class ExtractedChunk:
    source_label: str
    content: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    kind: str
    mime_type: str
    chunks: tuple[ExtractedChunk, ...]


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "iframe", "template", "noscript"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "div", "h1", "h2", "h3", "li", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "iframe", "template", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _clean_text(text: str, max_text_bytes: int) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").strip()
    if not text:
        raise ExtractionError("file has no extractable text")
    if len(text.encode("utf-8")) > max_text_bytes:
        raise ExtractionError("extracted text exceeds configured limit")
    if contains_sensitive_text(text):
        raise ExtractionError("file contains sensitive text")
    return text


def _chunks(
    filename: str, sections: list[tuple[str, str]], max_text_bytes: int
) -> tuple[ExtractedChunk, ...]:
    result: list[ExtractedChunk] = []
    total = 0
    for label, content in sections:
        clean = content.replace("\x00", "").replace("\r\n", "\n").strip()
        if not clean:
            continue
        total += len(clean.encode("utf-8"))
        if total > max_text_bytes:
            raise ExtractionError("extracted text exceeds configured limit")
        if contains_sensitive_text(clean):
            raise ExtractionError("file contains sensitive text")
        for offset in range(0, len(clean), MAX_CHUNK_CHARS):
            part = clean[offset : offset + MAX_CHUNK_CHARS]
            result.append(
                ExtractedChunk(f"{filename[:120]}: {label[:60]} {offset // MAX_CHUNK_CHARS + 1}", part)
            )
    if not result:
        raise ExtractionError("file has no extractable text")
    return tuple(result)


def _text_sections(extension: str, content: bytes, max_text_bytes: int) -> list[tuple[str, str]]:
    try:
        decoded = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtractionError("text file must be UTF-8") from exc
    if len(content) > max_text_bytes:
        raise ExtractionError("text file exceeds extraction limit")
    if extension == "json":
        try:
            json.loads(decoded)
        except ValueError as exc:
            raise ExtractionError("JSON file is malformed") from exc
    if extension == "xml":
        if re.search(r"<!\s*(DOCTYPE|ENTITY)", decoded, flags=re.I):
            raise ExtractionError("XML declarations with entities are unsupported")
        try:
            ET.fromstring(decoded)  # noqa: S314 - bounded input; entity declarations rejected above.
        except ET.ParseError as exc:
            raise ExtractionError("XML file is malformed") from exc
    if extension in {"html", "htm"}:
        parser = _VisibleHTML()
        parser.feed(decoded)
        decoded = "".join(parser.parts)
    if extension in {"csv", "tsv"}:
        delimiter = "," if extension == "csv" else "\t"
        try:
            rows = csv.reader(io.StringIO(decoded, newline=""), delimiter=delimiter, strict=True)
            decoded = "\n".join(
                f"row {index}: "
                + "; ".join(f"column {column}={value}" for column, value in enumerate(row, 1))
                for index, row in enumerate(rows, 1)
            )
        except csv.Error as exc:
            raise ExtractionError("table file is malformed") from exc
    return [("text", _clean_text(decoded, max_text_bytes))]


def _office_sections(extension: str, content: bytes) -> list[tuple[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        names = archive.namelist()
        if (
            len(names) > MAX_ZIP_MEMBERS
            or "[Content_Types].xml" not in names
            or OFFICE_PART[extension] not in names
        ):
            raise ExtractionError("Office package is malformed or excessive")
        total = 0
        for info in archive.infolist():
            path = Path(info.filename)
            if path.is_absolute() or ".." in path.parts or info.flag_bits & 1:
                raise ExtractionError("Office package has an unsafe member")
            if info.file_size > MAX_OFFICE_XML_MEMBER or (
                info.compress_size and info.file_size > 100 * info.compress_size
            ):
                raise ExtractionError("Office package exceeds decompression limit")
            total += info.file_size
            if total > MAX_ZIP_UNCOMPRESSED:
                raise ExtractionError("Office package exceeds decompression limit")
            if "vbaproject" in info.filename.casefold():
                raise ExtractionError("macro-enabled Office package is unsupported")
            if info.filename.endswith(".rels"):
                rel = archive.read(info)
                if re.search(rb"TargetMode\s*=\s*['\"]External['\"]", rel, re.I):
                    raise ExtractionError("Office package has an external reference")
            if any(part in info.filename.casefold() for part in ("activex", "embeddings/", "oleobject")):
                raise ExtractionError("embedded Office objects are unsupported")
        if extension == "docx":
            return [("document", _xml_text(archive.read("word/document.xml")))]
        if extension == "pptx":
            slides = sorted(name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            return [(f"slide {index}", _xml_text(archive.read(name))) for index, name in enumerate(slides, 1)]
        sheets = sorted(name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        shared = (
            _xml_values(archive.read("xl/sharedStrings.xml"), "si") if "xl/sharedStrings.xml" in names else []
        )
        result: list[tuple[str, str]] = []
        for index, name in enumerate(sheets, 1):
            root = _safe_xml(archive.read(name))
            cells: list[str] = []
            for cell in root.iter():
                if cell.tag.rsplit("}", 1)[-1] != "c":
                    continue
                reference = cell.attrib.get("r", "cell")
                value = next((item.text for item in cell.iter() if item.tag.rsplit("}", 1)[-1] == "v"), None)
                if value is None:
                    value = "".join(
                        item.text or "" for item in cell.iter() if item.tag.rsplit("}", 1)[-1] == "t"
                    )
                elif cell.attrib.get("t") == "s":
                    try:
                        value = shared[int(value)]
                    except (IndexError, ValueError) as exc:
                        raise ExtractionError("spreadsheet shared string is malformed") from exc
                if value:
                    cells.append(f"{reference}={value}")
            result.append((f"sheet {index}", "\n".join(cells)))
        return result
    except (zipfile.BadZipFile, RuntimeError, ET.ParseError) as exc:
        raise ExtractionError("Office package is malformed") from exc


def _safe_xml(content: bytes) -> ET.Element:
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", content, flags=re.I):
        raise ExtractionError("Office XML entity declaration is unsupported")
    return ET.fromstring(content)  # noqa: S314 - bounded ZIP member; entity declarations rejected above.


def _xml_text(content: bytes) -> str:
    root = _safe_xml(content)
    return " ".join(item.text or "" for item in root.iter() if item.tag.rsplit("}", 1)[-1] in {"t", "v"})


def _xml_values(content: bytes, member: str) -> list[str]:
    return [
        "".join(item.itertext())
        for item in _safe_xml(content).iter()
        if item.tag.rsplit("}", 1)[-1] == member
    ]


def _ocr_image(content: bytes) -> str:
    executable = shutil.which("tesseract")
    if executable is None:
        raise ExtractionError("local image OCR is unavailable")
    try:
        result = subprocess.run(  # noqa: S603 - no shell; bounded worker process.
            [executable, "stdin", "stdout"],
            input=content,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError("local image OCR failed or timed out") from exc
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise ExtractionError("local image OCR failed or exceeded its limit")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _pdf_page_count(content: bytes) -> int:
    executable = shutil.which("pdfinfo")
    if executable is None:
        raise ExtractionError("PDF page inspector unavailable")
    try:
        result = subprocess.run(  # noqa: S603 - no shell; bounded worker process.
            [executable, "-"],
            input=content,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError("PDF page inspection failed or timed out") from exc
    if result.returncode != 0:
        raise ExtractionError("PDF is malformed or encrypted")
    match = re.search(rb"(?m)^Pages:\s*(\d+)\s*$", result.stdout)
    if match is None:
        raise ExtractionError("PDF page count is unavailable")
    pages = int(match.group(1))
    if not 1 <= pages <= 50:
        raise ExtractionError("PDF exceeds page limit")
    return pages


def _rasterized_pdf_page(content: bytes, page: int) -> bytes:
    executable = shutil.which("pdftoppm")
    if executable is None:
        raise ExtractionError("scanned PDF renderer unavailable")
    with tempfile.TemporaryDirectory(prefix="onefoundry-pdf-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(content)
        prefix = Path(directory) / "page"
        try:
            result = subprocess.run(  # noqa: S603 - no shell; bounded worker process.
                [
                    executable,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-scale-to",
                    "1400",
                    "-png",
                    "-singlefile",
                    str(source),
                    str(prefix),
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExtractionError("scanned PDF rendering failed or timed out") from exc
        image = prefix.with_suffix(".png")
        if result.returncode != 0 or not image.is_file() or image.stat().st_size > 10 * 1024 * 1024:
            raise ExtractionError("scanned PDF page is malformed or excessive")
        return image.read_bytes()


def _pdf_sections(content: bytes) -> list[tuple[str, str]]:
    if not content.startswith(b"%PDF-"):
        raise ExtractionError("PDF signature does not match extension")
    page_count = _pdf_page_count(content)
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ExtractionError("PDF extractor unavailable")
    try:
        process = subprocess.run(  # noqa: S603 - fixed executable, no shell; restricted worker.
            [executable, "-layout", "-", "-"],
            input=content,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError("PDF extractor unavailable or timed out") from exc
    if process.returncode != 0:
        raise ExtractionError("PDF is malformed or encrypted")
    pages = process.stdout.decode("utf-8", errors="replace").split("\f")
    sections: list[tuple[str, str]] = []
    for index in range(1, page_count + 1):
        page = pages[index - 1] if index <= len(pages) else ""
        if not page.strip():
            page = _ocr_image(_rasterized_pdf_page(content, index))
        sections.append((f"page {index}", page))
    return sections


def _write_office_profile(profile: Path) -> None:
    """Disable document scripts and embedded active content before conversion."""
    user = profile / "user"
    user.mkdir(parents=True, mode=0o700)
    (user / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">\n'
        '  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">\n'
        '    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>\n'
        '    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>\n'
        '    <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>\n'
        '    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>\n'
        "  </item>\n"
        "</oor:items>\n",
        encoding="utf-8",
    )


def _legacy_pdf(extension: str, content: bytes) -> bytes:
    executable = shutil.which("libreoffice")
    if executable is None:
        raise ExtractionError("legacy Office converter unavailable")
    with tempfile.TemporaryDirectory(prefix="onefoundry-convert-") as directory:
        source = Path(directory) / f"source.{extension}"
        source.write_bytes(content)
        profile = Path(directory) / "profile"
        _write_office_profile(profile)
        try:
            process = subprocess.run(  # noqa: S603 - no shell; restricted worker.
                [
                    executable,
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    directory,
                    str(source),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExtractionError("legacy Office converter unavailable or timed out") from exc
        target = Path(directory) / "source.pdf"
        if process.returncode != 0 or not target.is_file() or target.stat().st_size > MAX_ZIP_UNCOMPRESSED:
            raise ExtractionError("legacy Office conversion failed")
        return target.read_bytes()


def _image_mime(extension: str, content: bytes) -> str:
    # Structural decoding lives in the worker image; magic checks also reject
    # obvious extension swaps before any visual provider preflight.
    expected = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}[extension]
    signature_matches = (
        (expected == "PNG" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (expected == "JPEG" and content.startswith(b"\xff\xd8\xff"))
        or (expected == "WEBP" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
    )
    if not signature_matches:
        raise ExtractionError("image signature does not match extension")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format != expected or image.width * image.height > 20_000_000:
                    raise ExtractionError("image dimensions or format are unsupported")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning) as exc:
        raise ExtractionError("image is malformed or excessive") from exc
    return {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[expected]


def extract_input(
    filename: str, content: bytes, *, max_text_bytes: int, require_image_ocr: bool = False
) -> ExtractionResult:
    if not filename or len(filename) > 255 or "/" in filename or "\\" in filename or "\x00" in filename:
        raise ExtractionError("file name is invalid")
    extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if extension not in ALL_EXTENSIONS:
        raise ExtractionError("unsupported file format")
    if not content:
        raise ExtractionError("file is empty")
    if extension in IMAGE_EXTENSIONS:
        mime = _image_mime(extension, content)
        ocr_text = _ocr_image(content) if require_image_ocr else ""
        chunks = _chunks(filename, [("OCR text", ocr_text)], max_text_bytes) if ocr_text else ()
        return ExtractionResult("visual", mime, chunks)
    if extension in LEGACY_EXTENSIONS:
        sections = _pdf_sections(_legacy_pdf(extension, content))
        mime_type = "application/pdf"
    elif extension == "pdf":
        sections = _pdf_sections(content)
        mime_type = "application/pdf"
    elif extension in OFFICE_PART:
        sections = _office_sections(extension, content)
        mime_type = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }[extension]
    else:
        sections = _text_sections(extension, content, max_text_bytes)
        mime_type = {
            "txt": "text/plain",
            "md": "text/markdown",
            "csv": "text/csv",
            "tsv": "text/tab-separated-values",
            "json": "application/json",
            "yaml": "text/yaml",
            "yml": "text/yaml",
            "xml": "application/xml",
            "html": "text/html",
            "htm": "text/html",
        }[extension]
    return ExtractionResult("text", mime_type, _chunks(filename, sections, max_text_bytes))
