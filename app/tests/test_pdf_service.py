"""Unit tests for PDF inspection / extraction and secure storage (no database)."""

import io
import shutil
import uuid
from pathlib import Path

import pytest

from app.services import pdf_service
from app.services.storage_service import EmptyFileError, FileTooLargeError, LocalStorage, StorageError
from app.tests import pdf_factory


def write(tmp_path: Path, data: bytes, name: str = "f.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ inspect
def test_inspect_valid_pdf(tmp_path: Path) -> None:
    info = pdf_service.inspect_pdf(write(tmp_path, pdf_factory.text_pdf(["a", "b", "c"], title="Report")))
    assert info.page_count == 3
    assert info.title == "Report"


def test_inspect_rejects_non_pdf(tmp_path: Path) -> None:
    with pytest.raises(pdf_service.NotAPdfError):
        pdf_service.inspect_pdf(write(tmp_path, b"hello, I am a text file pretending to be a PDF"))


def test_inspect_rejects_corrupt_pdf(tmp_path: Path) -> None:
    with pytest.raises(pdf_service.CorruptPdfError):
        pdf_service.inspect_pdf(write(tmp_path, pdf_factory.corrupt_pdf()))


def test_inspect_rejects_password_protected_pdf(tmp_path: Path) -> None:
    with pytest.raises(pdf_service.EncryptedPdfError):
        pdf_service.inspect_pdf(write(tmp_path, pdf_factory.encrypted_pdf()))


def test_inspect_rejects_too_many_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_pdf_pages", 2)
    with pytest.raises(pdf_service.TooManyPagesError):
        pdf_service.inspect_pdf(write(tmp_path, pdf_factory.blank_pdf(3)))


# ------------------------------------------------------------------ extract
def test_extract_text_keeps_accurate_page_numbers(tmp_path: Path) -> None:
    path = write(tmp_path, pdf_factory.text_pdf(["Alpha content here.", "", "Gamma content here."]))
    result = pdf_service.extract_text(path)
    assert result.total_pages == 3
    assert [(p.page_number, p.text) for p in result.pages] == [
        (1, "Alpha content here."),
        (3, "Gamma content here."),
    ]
    assert result.empty_pages == [2]


def test_extract_blank_pdf_raises_no_text(tmp_path: Path) -> None:
    with pytest.raises(pdf_service.NoExtractableTextError):
        pdf_service.extract_text(write(tmp_path, pdf_factory.blank_pdf(3)))


def test_extract_scanned_pdf_raises_no_text_when_ocr_disabled(tmp_path: Path) -> None:
    with pytest.raises(pdf_service.NoExtractableTextError, match="scanned"):
        pdf_service.extract_text(write(tmp_path, pdf_factory.image_only_pdf()), ocr=False)


def test_ocr_without_tesseract_degrades_gracefully(tmp_path: Path) -> None:
    """OCR enabled but Tesseract missing -> no crash, just 'no text'."""
    with pytest.raises(pdf_service.NoExtractableTextError):
        pdf_service.extract_text(write(tmp_path, pdf_factory.image_only_pdf(1)), ocr=True)


def test_ocr_runs_only_for_image_pages_without_text(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(pdf_service, "_ocr_page", lambda page, lang: (calls.append(page.number + 1), "text recognised by ocr")[1])
    scanned = pdf_service.extract_text(write(tmp_path, pdf_factory.scanned_pdf(["Scanned page text"])), ocr=True)
    assert calls == [1]
    assert scanned.pages[0].used_ocr and scanned.pages[0].text == "text recognised by ocr"

    calls.clear()
    normal = pdf_service.extract_text(write(tmp_path, pdf_factory.text_pdf(["This page has a real text layer."]), "t.pdf"), ocr=True)
    assert calls == []  # a page with a text layer never pays for OCR
    assert not normal.pages[0].used_ocr


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed (it is in the Docker image)")
def test_real_ocr_reads_a_scanned_page(tmp_path: Path) -> None:
    result = pdf_service.extract_text(
        write(tmp_path, pdf_factory.scanned_pdf(["The monthly parking fee is INR 300."])), ocr=True
    )
    assert "parking fee" in result.pages[0].text.lower()


def test_iter_pages_is_lazy(tmp_path: Path) -> None:
    pages = pdf_service.iter_pages(write(tmp_path, pdf_factory.text_pdf(["one", "two"])))
    assert next(pages).page_number == 1  # generator: pages are produced one at a time
    pages.close()


def test_clean_text() -> None:
    raw = "Hello\x00 world  \r\n\r\n\r\n\r\nNext\x07 para   \n"
    assert pdf_service.clean_text(raw) == "Hello world\n\nNext para"


def test_pdf_signature() -> None:
    assert pdf_service.has_pdf_signature(b"%PDF-1.7 ...")
    assert pdf_service.has_pdf_signature(b"\xef\xbb\xbf junk %PDF-1.4")
    assert not pdf_service.has_pdf_signature(b"MZ\x90\x00 windows exe")


# ------------------------------------------------------------------ storage
def test_storage_streams_and_hashes(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    owner = uuid.uuid4()
    stored = storage.save_stream(io.BytesIO(b"%PDF-1.7 data"), owner, max_bytes=1000)
    assert stored.key.startswith(f"{owner}/") and stored.key.endswith(".pdf")
    assert stored.path.read_bytes() == b"%PDF-1.7 data"
    assert len(stored.sha256) == 64
    assert storage.exists(stored.key)
    assert storage.delete(stored.key) is True
    assert storage.delete(stored.key) is False  # idempotent


def test_storage_enforces_size_limit_and_cleans_up(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    with pytest.raises(FileTooLargeError):
        storage.save_stream(io.BytesIO(b"x" * 5000), uuid.uuid4(), max_bytes=1000)
    assert list(tmp_path.rglob("*.*")) == []  # no partial file left behind


def test_storage_rejects_empty_file(tmp_path: Path) -> None:
    with pytest.raises(EmptyFileError):
        LocalStorage(tmp_path).save_stream(io.BytesIO(b""), uuid.uuid4(), max_bytes=1000)


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "..\\..\\windows\\win.ini",
        f"{uuid.uuid4()}/../../secret.pdf",
        "/absolute/path.pdf",
        f"{uuid.uuid4()}/{'a' * 32}.exe",
    ],
)
def test_storage_blocks_path_traversal(tmp_path: Path, key: str) -> None:
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).path_for(key)
