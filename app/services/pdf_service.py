"""PDF inspection and text extraction with PyMuPDF.

Two operations:
  * inspect_pdf()  - cheap validation at upload time (opens the file, reads the
                     page count, checks encryption). Does NOT extract text.
  * extract_text() - full page-by-page extraction, used by the background
                     processing pipeline (Phase 5/7).

Page numbers are 1-based everywhere (what humans see in a PDF viewer), so
citations can be trusted.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PDF_MAGIC = b"%PDF-"


# ------------------------------------------------------------------ errors
class PdfError(Exception):
    """Base class. `message` is safe to show to users."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAPdfError(PdfError):
    pass


class CorruptPdfError(PdfError):
    pass


class EncryptedPdfError(PdfError):
    pass


class EmptyPdfError(PdfError):
    pass


class TooManyPagesError(PdfError):
    pass


class NoExtractableTextError(PdfError):
    """PDF opens fine but has (almost) no text layer - usually a scanned document."""


# ------------------------------------------------------------------ results
@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    title: str | None


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based
    text: str
    used_ocr: bool = False


@dataclass
class ExtractionResult:
    total_pages: int
    pages: list[PageText] = field(default_factory=list)  # only pages that have text
    empty_pages: list[int] = field(default_factory=list)  # 1-based numbers with no text

    @property
    def total_chars(self) -> int:
        return sum(len(p.text) for p in self.pages)


# ------------------------------------------------------------------ helpers
def has_pdf_signature(header: bytes) -> bool:
    """Magic bytes check. The spec allows junk before %PDF- within the first 1 KB."""
    return PDF_MAGIC in header[:1024]


_MULTI_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str) -> str:
    text = _CONTROL_CHARS.sub("", text)  # NUL bytes would break PostgreSQL TEXT
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACES.sub("\n", text)
    text = _MULTI_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _open(path: Path) -> pymupdf.Document:
    doc: pymupdf.Document | None = None
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several internal error types
        # str(exc), not exc: log handlers may retain the record's args.
        logger.info("PyMuPDF could not open %s: %s", path.name, str(exc))
    # Raised OUTSIDE the except block and without `from exc` on purpose:
    # PyMuPDF's exception traceback references its internal file object.
    # Keeping it alive (via exception chaining) keeps the file handle OPEN,
    # and on Windows an open file cannot be deleted -> the rejected upload
    # could never be cleaned up.
    if doc is None:
        raise CorruptPdfError("The file is not a valid or readable PDF")
    if doc.needs_pass:
        doc.close()
        raise EncryptedPdfError("Password-protected PDFs are not supported. Remove the password and upload again.")
    return doc


# ------------------------------------------------------------------ public API
def inspect_pdf(path: Path) -> PdfInfo:
    with path.open("rb") as fh:
        if not has_pdf_signature(fh.read(1024)):
            raise NotAPdfError("The file content is not a PDF")

    doc = _open(path)
    try:
        page_count = doc.page_count
        if page_count == 0:
            raise EmptyPdfError("The PDF has no pages")
        max_pages = get_settings().max_pdf_pages
        if page_count > max_pages:
            raise TooManyPagesError(f"The PDF has {page_count} pages; the limit is {max_pages}")
        # Touch the first page: catches files whose header is fine but whose
        # page tree is broken.
        try:
            doc.load_page(0)
            readable = True
        except Exception:
            readable = False
        if not readable:
            raise CorruptPdfError("The PDF is damaged and its pages cannot be read")
        title = (doc.metadata or {}).get("title") or None
        return PdfInfo(page_count=page_count, title=title)
    finally:
        doc.close()


def iter_pages(path: Path, *, ocr: bool | None = None) -> Iterator[PageText]:
    """Yield pages one at a time - only one page is in memory at once."""
    settings = get_settings()
    ocr = settings.ocr_enabled if ocr is None else ocr
    flags = pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_DEHYPHENATE

    doc = _open(path)
    try:
        for index in range(doc.page_count):
            try:
                page = doc.load_page(index)
                text = clean_text(page.get_text("text", flags=flags, sort=True))
            except Exception:
                logger.warning("Could not read page %d; treating as empty", index + 1, exc_info=True)
                yield PageText(page_number=index + 1, text="")
                continue

            used_ocr = False
            if not text and ocr and page.get_images():
                text, used_ocr = _ocr_page(page, settings.ocr_language), True
            yield PageText(page_number=index + 1, text=text, used_ocr=used_ocr)
    finally:
        doc.close()


def _ocr_page(page: pymupdf.Page, language: str) -> str:
    """OCR one page via Tesseract. Returns "" if Tesseract is not available."""
    try:
        textpage = page.get_textpage_ocr(language=language, dpi=300, full=True)
        return clean_text(page.get_text("text", textpage=textpage, sort=True))
    except Exception:
        logger.warning("OCR failed on page %d (is Tesseract installed?)", page.number + 1, exc_info=True)
        return ""


def extract_text(path: Path, *, ocr: bool | None = None) -> ExtractionResult:
    """Extract every page. Raises NoExtractableTextError for scanned/blank PDFs."""
    result = ExtractionResult(total_pages=0)
    for page in iter_pages(path, ocr=ocr):
        result.total_pages = page.page_number
        if page.text:
            result.pages.append(page)
        else:
            result.empty_pages.append(page.page_number)

    if result.total_chars < get_settings().min_extractable_chars:
        raise NoExtractableTextError(
            "No extractable text found. The PDF may be scanned or contain only images; "
            "enable OCR to process scanned documents."
        )
    return result
