"""DOCX / TXT / Markdown -> PDF conversion at upload time.

Why convert instead of a separate pipeline per format?
------------------------------------------------------
Everything downstream speaks "PDF pages": chunking keeps page offsets,
citations say "p. 3", the in-app viewer opens the file at a page. Converting
once at upload means all of that works unchanged for every format.

Pages of a DOCX/TXT/MD file are *virtual* (they do not exist in the source),
so they are defined by the converted PDF: "p. 3" means page 3 of the PDF the
user sees in the viewer - citations and viewer always agree.

Why a ".pages.json" sidecar?
----------------------------
Re-extracting text from the generated PDF is lossy for some scripts (e.g.
Devanagari comes back garbled because of how complex glyphs are mapped back
to characters). We already HAVE the exact text, so while laying out the PDF we
record which page every block lands on (PyMuPDF Story.element_positions) and
store "page -> original text" next to the PDF. The processing pipeline reads
that sidecar instead of the PDF text layer.

Security
--------
* DOCX is a ZIP: the total uncompressed size and entry count are checked
  BEFORE parsing (zip bomb protection).
* Text files must not contain NUL bytes (binary files renamed to .txt).
* Markdown is rendered with raw HTML and images disabled: user content can
  never inject markup into the layout engine or make it load files.
"""

import html
import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.storage_service import sidecar_path

logger = get_logger(__name__)

# extension -> source type
EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md", ".markdown": "md"}
GENERIC_TYPES = {"application/octet-stream", ""}
CONTENT_TYPES = {
    "pdf": {"application/pdf", "application/x-pdf"} | GENERIC_TYPES,
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"} | GENERIC_TYPES,
    "txt": {"text/plain"} | GENERIC_TYPES,
    "md": {"text/markdown", "text/x-markdown", "text/plain"} | GENERIC_TYPES,
}
SUPPORTED_LABEL = "PDF, DOCX, TXT or Markdown"

MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB
MAX_DOCX_ENTRIES = 10_000
CHARS_PER_PAGE_ESTIMATE = 3500   # cheap early "too many pages" check before layout
MAX_BLOCK_CHARS = 1500           # long text paragraphs are split so page mapping stays precise

PAGE = pymupdf.paper_rect("a4")
MARGIN = 54  # 0.75 inch
CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.35; }
h1 { font-size: 18pt; } h2 { font-size: 15pt; } h3 { font-size: 13pt; }
h4, h5, h6 { font-size: 11pt; }
pre, code { font-family: monospace; font-size: 9.5pt; }
table { border-collapse: collapse; } td, th { border: 1px solid #999; padding: 2pt 4pt; }
"""


class ConversionError(Exception):
    """`message` is safe to show to users."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedFormatError(ConversionError):
    pass


@dataclass(frozen=True)
class Block:
    html: str  # HTML fragment for the layout
    text: str  # exact plain text, used for search


@dataclass(frozen=True)
class Converted:
    page_count: int
    pages: list[str]  # pages[i] = original text of page i+1 ("" = no text)


def source_type_for(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    return EXTENSIONS.get(suffix)


def content_type_ok(source_type: str, content_type: str | None) -> bool:
    return (content_type or "").split(";")[0].strip().lower() in CONTENT_TYPES[source_type]


# ------------------------------------------------------------------ public API
def convert_in_place(path: Path, source_type: str) -> Converted:
    """Replace the uploaded DOCX/TXT/MD file at `path` with a PDF and write the
    page-text sidecar next to it. Raises ConversionError for bad input."""
    blocks = _read_blocks(path, source_type)
    if not any(b.text.strip() for b in blocks):
        raise ConversionError("The file contains no text")
    max_pages = get_settings().max_pdf_pages
    if sum(len(b.text) for b in blocks) > max_pages * CHARS_PER_PAGE_ESTIMATE:
        raise ConversionError(f"The document is too long; the limit is {max_pages} pages")

    pdf_bytes, pages = _layout(blocks)
    if len(pages) > max_pages:
        raise ConversionError(f"The document has {len(pages)} pages; the limit is {max_pages}")

    tmp = path.with_suffix(".conv")
    try:
        tmp.write_bytes(pdf_bytes)
        tmp.replace(path)  # atomic: the key now holds the PDF
        sidecar_path(path).write_text(json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)
    return Converted(page_count=len(pages), pages=pages)


def iter_sidecar_pages(pdf_path: Path) -> Iterator[tuple[int, str]]:
    """(page number, text) for a converted document, in page order."""
    data = json.loads(sidecar_path(pdf_path).read_text(encoding="utf-8"))
    for number, text in enumerate(data["pages"], start=1):
        yield number, text


# ------------------------------------------------------------------ layout
def _layout(blocks: list[Block]) -> tuple[bytes, list[str]]:
    """Lay the blocks out on A4 pages; record the page each block starts on."""
    body = "".join(f'<div id="b{i}">{b.html}</div>' for i, b in enumerate(blocks))
    story = pymupdf.Story(html=body, user_css=CSS)
    first_page: dict[int, int] = {}

    def on_position(pos) -> None:  # noqa: ANN001 - pymupdf.ElementPosition
        if pos.id and pos.id.startswith("b") and pos.open_close & 1:
            first_page.setdefault(int(pos.id[1:]), pos.pageno)

    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    page_no, more = 0, True
    while more:
        page_no += 1
        device = writer.begin_page(PAGE)
        more, _ = story.place(PAGE + (MARGIN, MARGIN, -MARGIN, -MARGIN))
        story.element_positions(on_position, {"pageno": page_no})  # BEFORE draw()
        story.draw(device)
        writer.end_page()
    writer.close()

    pages = [[] for _ in range(page_no)]
    current = 1
    for i, block in enumerate(blocks):
        current = first_page.get(i, current)  # never seen -> same page as the previous block
        if block.text.strip():
            pages[current - 1].append(block.text.strip())
    return buffer.getvalue(), ["\n\n".join(p) for p in pages]


# ------------------------------------------------------------------ readers
def _read_blocks(path: Path, source_type: str) -> list[Block]:
    if source_type == "docx":
        return _docx_blocks(path)
    text = _decode_text(path.read_bytes())
    return _markdown_blocks(text) if source_type == "md" else _text_blocks(text)


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise ConversionError("The text file could not be decoded") from None
    if b"\x00" in data:
        raise UnsupportedFormatError("The file is not a text file")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")  # never fails: every byte is a character


def _split_long(text: str) -> list[str]:
    """Split a long paragraph at line (or word) boundaries."""
    if len(text) <= MAX_BLOCK_CHARS:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_BLOCK_CHARS:  # one very long line: cut at a space
            cut = line.rfind(" ", 0, MAX_BLOCK_CHARS)
            cut = cut if cut > 0 else MAX_BLOCK_CHARS
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        if current and len(current) + len(line) + 1 > MAX_BLOCK_CHARS:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def _text_blocks(text: str) -> list[Block]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip("\n")
        if not paragraph.strip():
            continue
        for part in _split_long(paragraph):
            body = "<br/>".join(html.escape(line) for line in part.split("\n"))
            blocks.append(Block(f"<p>{body}</p>", part))
    return blocks


def _markdown_blocks(text: str) -> list[Block]:
    from markdown_it import MarkdownIt

    # No raw HTML, no images: user content cannot inject markup or file references.
    md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    tokens = md.parse(text)
    blocks: list[Block] = []
    start = 0
    for i, token in enumerate(tokens):
        if token.level == 0 and token.nesting <= 0:  # a top-level block ends here
            group = tokens[start:i + 1]
            start = i + 1
            plain = "\n".join(filter(None, (_token_text(t) for t in group)))
            blocks.append(Block(md.renderer.render(group, md.options, {}), plain))
    return blocks


def _token_text(token) -> str:  # noqa: ANN001 - markdown_it Token
    if token.type == "inline":
        out = []
        for child in token.children or []:
            if child.type in ("text", "code_inline"):
                out.append(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                out.append("\n")
        return "".join(out)
    if token.type in ("fence", "code_block"):
        return token.content.rstrip("\n")
    return ""


def _docx_blocks(path: Path) -> list[Block]:
    _check_zip(path)
    import docx
    from docx.opc.exceptions import PackageNotFoundError
    from docx.table import Table

    # Parsed from memory (the upload is size-limited), not from the path: an
    # exception traceback would otherwise keep the file handle open, and on
    # Windows an open file cannot be deleted -> the rejected upload would stay.
    document = None
    try:
        document = docx.Document(io.BytesIO(path.read_bytes()))
    except (PackageNotFoundError, KeyError, ValueError, SyntaxError, zipfile.BadZipFile):
        pass  # lxml's XMLSyntaxError is a SyntaxError subclass
    if document is None:
        raise ConversionError("The file is not a valid Word document (.docx)")

    blocks: list[Block] = []
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            items = "".join(f"<li>{html.escape(t)}</li>" for t in list_items)
            blocks.append(Block(f"<ul>{items}</ul>", "\n".join(f"- {t}" for t in list_items)))
            list_items.clear()

    for item in document.iter_inner_content():
        if isinstance(item, Table):
            flush_list()
            blocks.append(_docx_table(item))
            continue
        text = item.text.strip()
        if not text:
            continue
        style = (item.style.name if item.style is not None else "") or ""
        if style.startswith("List") or item._p.pPr is not None and item._p.pPr.numPr is not None:
            list_items.append(text)
            continue
        flush_list()
        level = _heading_level(style)
        tag = f"h{level}" if level else "p"
        for part in _split_long(text) if not level else [text]:
            body = "<br/>".join(html.escape(line) for line in part.split("\n"))
            blocks.append(Block(f"<{tag}>{body}</{tag}>", part))
    flush_list()
    return blocks


def _heading_level(style: str) -> int:
    if style == "Title":
        return 1
    if style.startswith("Heading "):
        suffix = style.removeprefix("Heading ").strip()
        if suffix.isdigit():
            return min(max(int(suffix), 1), 6)
    return 0


def _docx_table(table) -> Block:  # noqa: ANN001 - docx Table
    rows_html, rows_text = [], []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        rows_html.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
        rows_text.append(" | ".join(cells))
    return Block(f"<table>{''.join(rows_html)}</table>", "\n".join(rows_text))


def _check_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile:
        raise UnsupportedFormatError("The file is not a valid Word document (.docx)") from None
    if len(entries) > MAX_DOCX_ENTRIES or sum(e.file_size for e in entries) > MAX_DOCX_UNCOMPRESSED:
        raise ConversionError("The Word document is too large when uncompressed")
    if not any(e.filename == "word/document.xml" for e in entries):
        raise UnsupportedFormatError("The file is not a valid Word document (.docx)")
