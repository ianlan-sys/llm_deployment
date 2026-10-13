import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


DEFAULT_MAX_SECTION_CHARS = 4_000
SUPPORTED_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_HEADING = re.compile(
    r"^\s*(?:[一二三四五六七八九十百]+、|（[一二三四五六七八九十百]+）|"
    r"第[一二三四五六七八九十百]+[章节部分]|\d+[、.．])\s*\S+"
)


class DocumentParseError(ValueError):
    """Raised when an uploaded document cannot produce reviewable text."""


@dataclass(frozen=True)
class ParsedSection:
    title: str
    content: str


@dataclass(frozen=True)
class ParsedDocument:
    sections: list[ParsedSection]


def parse_document(
    path: Path,
    media_type: str,
    max_section_chars: int = DEFAULT_MAX_SECTION_CHARS,
) -> ParsedDocument:
    """Extract and section a supported UTF-8 TXT, PDF, or DOCX document."""
    if max_section_chars <= 0:
        raise ValueError("max_section_chars must be greater than zero")
    extension = path.suffix.lower()
    expected_media_type = SUPPORTED_MEDIA_TYPES.get(extension)
    if expected_media_type is None:
        raise DocumentParseError("不支持的文档扩展名")
    if media_type != expected_media_type:
        raise DocumentParseError("文件扩展名与 MIME 类型不匹配")

    if extension == ".txt":
        text = _read_txt(path)
    elif extension == ".pdf":
        text = _read_pdf(path)
    else:
        text = _read_docx(path)
    if not text.strip():
        raise DocumentParseError("文档中没有可提取文本；扫描版 PDF 需要 OCR")
    return ParsedDocument(sections=_split_sections(text, max_section_chars))


def _read_txt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentParseError("TXT 文件必须使用 UTF-8 编码") from exc
    except OSError as exc:
        raise DocumentParseError("无法读取文档") from exc


def _read_pdf(path: Path) -> str:
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as exc:
        raise DocumentParseError("无法解析 PDF 文档") from exc


def _read_docx(path: Path) -> str:
    try:
        document = Document(path)
        lines: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                lines.append(Paragraph(child, document).text)
            elif child.tag.endswith("}tbl"):
                table = Table(child, document)
                lines.extend(
                    "\t".join(cell.text for cell in row.cells) for row in table.rows
                )
        return "\n".join(lines)
    except Exception as exc:
        raise DocumentParseError("无法解析 DOCX 文档") from exc


def _split_sections(text: str, max_section_chars: int) -> list[ParsedSection]:
    groups: list[tuple[str, list[str]]] = []
    title = "正文"
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _HEADING.match(line):
            if any(item for item in lines):
                groups.append((title, lines))
            title = line
            lines = [line]
        else:
            lines.append(line)
    if any(item for item in lines):
        groups.append((title, lines))

    sections: list[ParsedSection] = []
    for section_title, section_lines in groups:
        content = "\n".join(section_lines).strip()
        for index, start in enumerate(range(0, len(content), max_section_chars)):
            chunk_title = (
                section_title if index == 0 else f"{section_title}（续{index}）"
            )
            sections.append(
                ParsedSection(
                    title=chunk_title,
                    content=content[start : start + max_section_chars],
                )
            )
    if not sections:
        raise DocumentParseError("文档中没有可提取文本；扫描版 PDF 需要 OCR")
    return sections
