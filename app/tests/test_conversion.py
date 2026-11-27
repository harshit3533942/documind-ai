"""DOCX / TXT / Markdown upload: conversion to PDF + exact page text (sidecar)."""

import io
import json
import uuid
import zipfile
from collections.abc import Callable

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentChunk, DocumentStatus, User
from app.services import conversion_service
from app.services.chunking_service import ChunkingConfig, TextChunker
from app.services.document_processing_service import DocumentProcessor
from app.services.embedding_service import HashingEmbedder
from app.services.storage_service import LocalStorage, sidecar_path
from app.tests import pdf_factory

DOCS = "/api/v1/documents"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def alice(create_user: Callable[..., User], auth_headers) -> dict[str, str]:
    create_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def processor(db_session: Session, storage: LocalStorage) -> DocumentProcessor:
    return DocumentProcessor(
        db_session, storage, TextChunker(ChunkingConfig(chunk_size=60, chunk_overlap=15)), embedder=HashingEmbedder()
    )


def upload(client, headers, name: str, data: bytes, content_type: str):
    return client.post(DOCS, headers=headers, files={"file": (name, data, content_type)})


def all_text(db: Session, doc_id: uuid.UUID) -> str:
    rows = db.scalars(select(DocumentChunk.content).where(DocumentChunk.document_id == doc_id))
    return " ".join(rows)


def stored(db: Session, storage: LocalStorage, doc_id: str):
    doc = db.get(Document, uuid.UUID(doc_id))
    return doc, storage.path_for(doc.storage_key)


def stored_files(storage: LocalStorage) -> list:
    return [p for p in storage.root.rglob("*") if p.is_file()] if storage.root.exists() else []


# ------------------------------------------------------------------ TXT
def test_txt_upload_is_converted_and_keeps_exact_text(client, alice, storage, db_session, processor) -> None:
    text = "Leave policy\n\nकर्मचारियों को 24 दिन की छुट्टी मिलती है।\n\nRemote work: three days per week."
    resp = upload(client, alice, "policy.txt", text.encode("utf-8"), "text/plain")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_type"] == "txt"
    assert body["mime_type"] == "application/pdf"  # the stored file is a PDF
    assert body["total_pages"] == 1
    assert body["file_size"] == len(text.encode("utf-8"))  # size of what the user uploaded

    doc, path = stored(db_session, storage, body["id"])
    assert path.read_bytes().startswith(b"%PDF-")
    assert sidecar_path(path).is_file()

    result = processor.process(doc.id)
    assert result.status == "completed"
    content = all_text(db_session, doc.id)
    # Devanagari is garbled when re-extracted from a PDF; the sidecar keeps it exact.
    assert "कर्मचारियों को 24 दिन की छुट्टी मिलती है।" in content
    assert "three days per week" in content


@pytest.mark.parametrize(("encoding", "raw"), [
    ("utf-16", "Café menu: crème brûlée".encode("utf-16")),
    ("cp1252", "Café menu: crème brûlée".encode("cp1252")),
    ("utf-8-sig", "Café menu: crème brûlée".encode("utf-8-sig")),
])
def test_txt_encodings(client, alice, db_session, processor, encoding, raw) -> None:
    resp = upload(client, alice, f"{encoding}.txt", raw, "text/plain")
    assert resp.status_code == 201, resp.text
    doc_id = uuid.UUID(resp.json()["id"])
    processor.process(doc_id)
    assert "Café menu: crème brûlée" in all_text(db_session, doc_id)


def test_long_txt_maps_text_to_the_right_pages(client, alice, storage, db_session, processor) -> None:
    paragraphs = [f"Paragraph {i}. " + "Filler words to take up space on the page. " * 12 for i in range(60)]
    resp = upload(client, alice, "long.txt", "\n\n".join(paragraphs).encode(), "text/plain")
    assert resp.status_code == 201, resp.text
    doc, path = stored(db_session, storage, resp.json()["id"])
    assert doc.total_pages > 3

    pages = json.loads(sidecar_path(path).read_text(encoding="utf-8"))["pages"]
    pdf = pymupdf.open(path)
    try:
        assert len(pages) == pdf.page_count
        for number in (0, pdf.page_count // 2, pdf.page_count - 1):
            first_label = pages[number].split(".")[0]    # e.g. "Paragraph 37"
            assert first_label in pdf[number].get_text()  # the viewer shows it on that page
    finally:
        pdf.close()

    processor.process(doc.id)
    last = db_session.scalars(
        select(DocumentChunk).where(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.chunk_index.desc())
    ).first()
    assert last.page_end == doc.total_pages


def test_txt_without_blank_lines_is_split_into_blocks() -> None:
    blocks = conversion_service._text_blocks("\n".join(f"line {i} " + "x" * 80 for i in range(100)))
    assert len(blocks) > 3
    assert all(len(b.text) <= conversion_service.MAX_BLOCK_CHARS for b in blocks)
    one_huge_line = conversion_service._split_long("word " * 2000)
    assert all(len(p) <= conversion_service.MAX_BLOCK_CHARS for p in one_huge_line)
    assert conversion_service._split_long("y" * 4000)[0] == "y" * conversion_service.MAX_BLOCK_CHARS


# ------------------------------------------------------------------ Markdown
def test_markdown_is_rendered_and_raw_html_is_not_markup(client, alice, db_session, storage, processor) -> None:
    md = (
        "# Travel policy\n\nThe **daily allowance** is `INR 1,500`.\n\n"
        "- Book trains early\n- Keep receipts\n\n"
        "| City | Rate |\n|---|---|\n| Pune | 1500 |\n\n"
        "```\nclaim --amount 1500\n```\n\n"
        "<script>alert('x')</script>\n\n![logo](http://example.com/x.png)\n"
    )
    resp = upload(client, alice, "travel.md", md.encode(), "text/markdown")
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_type"] == "md"
    doc, path = stored(db_session, storage, resp.json()["id"])
    pdf = pymupdf.open(path)
    pdf_text = pdf[0].get_text()
    pdf.close()
    assert "Travel policy" in pdf_text and "**" not in pdf_text  # markdown rendered, not shown raw
    assert "<script>" in pdf_text                                # raw HTML shown as text, never parsed

    processor.process(doc.id)
    content = all_text(db_session, doc.id)
    assert "The daily allowance is INR 1,500." in content
    assert "claim --amount 1500" in content
    assert "**" not in content


# ------------------------------------------------------------------ DOCX
def test_docx_upload_with_headings_lists_and_tables(client, alice, db_session, processor) -> None:
    data = pdf_factory.docx_file([
        ("h1", "Employee Handbook"),
        ("p", "All full-time employees receive 24 days of paid annual leave per year."),
        ("li", "Submit leave requests in advance"),
        ("li", "Manager approval is required"),
        ("table", "Grade|Days;Junior|20;Senior|28"),
        ("p", ""),
    ])
    resp = upload(client, alice, "handbook.docx", data, DOCX_TYPE)
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_type"] == "docx"
    doc_id = uuid.UUID(resp.json()["id"])
    processor.process(doc_id)
    content = all_text(db_session, doc_id)
    for expected in ("Employee Handbook", "24 days of paid annual leave", "- Manager approval is required", "Senior | 28"):
        assert expected in content


def test_heading_levels() -> None:
    assert conversion_service._heading_level("Title") == 1
    assert conversion_service._heading_level("Heading 2") == 2
    assert conversion_service._heading_level("Heading 9") == 6
    assert conversion_service._heading_level("Heading x") == 0
    assert conversion_service._heading_level("Normal") == 0


# ------------------------------------------------------------------ rejections
def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in entries.items():
            z.writestr(name, value)
    return buffer.getvalue()


@pytest.mark.parametrize(("name", "content_type", "data", "expected", "message"), [
    ("program.exe", "application/octet-stream", b"MZ\x90", 415, "Only PDF, DOCX, TXT or Markdown"),
    ("notes.txt", "image/png", b"hello", 415, "Only PDF"),
    ("binary.txt", "text/plain", b"abc\x00\x01\x02", 415, "not a text file"),
    ("fake.docx", DOCX_TYPE, b"just text, not a zip", 415, "not a valid Word document"),
    ("other.docx", DOCX_TYPE, _zip({"hello.txt": b"hi"}), 415, "not a valid Word document"),
    ("broken.docx", DOCX_TYPE, _zip({"word/document.xml": b"<broken"}), 422, "not a valid Word document"),
    ("blank.txt", "text/plain", b"   \n\n  \n", 422, "contains no text"),
    ("blank.md", "text/markdown", b"---\n\n***\n", 422, "contains no text"),  # only horizontal rules
])
def test_rejected_files_leave_nothing_behind(client, alice, storage, db_session, name, content_type, data, expected, message) -> None:
    resp = upload(client, alice, name, data, content_type)
    assert resp.status_code == expected, resp.text
    assert message in resp.json()["error"]["message"]
    assert stored_files(storage) == []
    assert db_session.scalar(select(Document.id)) is None


def test_docx_zip_bomb_is_rejected_before_parsing(client, alice, monkeypatch) -> None:
    monkeypatch.setattr(conversion_service, "MAX_DOCX_UNCOMPRESSED", 1000)
    resp = upload(client, alice, "bomb.docx", _zip({"word/document.xml": b"0" * 5000}), DOCX_TYPE)
    assert resp.status_code == 422
    assert "too large when uncompressed" in resp.json()["error"]["message"]


def test_too_many_pages(client, alice, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_pdf_pages", 2)
    early = upload(client, alice, "huge.txt", ("word " * 4000).encode(), "text/plain")  # > 2 * 3500 chars
    assert early.status_code == 422 and "too long" in early.json()["error"]["message"]
    paragraphs = "\n\n".join("Short line." for _ in range(400))  # few chars, many lines -> many pages
    late = upload(client, alice, "tall.txt", paragraphs.encode(), "text/plain")
    assert late.status_code == 422 and "the limit is 2" in late.json()["error"]["message"]


def test_duplicate_is_detected_on_the_original_file(client, alice) -> None:
    assert upload(client, alice, "a.txt", b"Same text content here.", "text/plain").status_code == 201
    second = upload(client, alice, "b.txt", b"Same text content here.", "text/plain")
    assert second.status_code == 409 and "a.txt" in second.json()["error"]["message"]


# ------------------------------------------------------------------ lifecycle
def test_delete_removes_pdf_and_sidecar(client, alice, storage, db_session) -> None:
    resp = upload(client, alice, "notes.md", b"# Notes\n\nSome notes.", "text/markdown")
    _, path = stored(db_session, storage, resp.json()["id"])
    assert client.delete(f"{DOCS}/{resp.json()['id']}", headers=alice, params={"permanent": "true"}).status_code == 204
    assert not path.exists() and not sidecar_path(path).exists()


def test_missing_sidecar_fails_with_clear_message(client, alice, storage, db_session, processor) -> None:
    resp = upload(client, alice, "notes.txt", b"Some useful notes about the project.", "text/plain")
    doc, path = stored(db_session, storage, resp.json()["id"])
    sidecar_path(path).unlink()
    result = processor.process(doc.id)
    assert result.status == "failed"
    db_session.refresh(doc)
    assert doc.status == DocumentStatus.FAILED
    assert "converted text" in doc.error_message


def test_viewer_serves_the_converted_pdf(client, alice) -> None:
    resp = upload(client, alice, "notes.txt", b"Viewer text.", "text/plain")
    file = client.get(f"{DOCS}/{resp.json()['id']}/file", headers=alice)
    assert file.status_code == 200
    assert file.headers["content-type"] == "application/pdf"
    assert file.content.startswith(b"%PDF-")
