"""Generate real PDF files for tests (no binary fixtures committed to git)."""

import pymupdf


def text_pdf(pages: list[str], title: str | None = None, fontsize: float = 11) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        # insert_textbox writes NOTHING (returns < 0) if the text does not fit -
        # fail loudly so a test can never silently run against an empty page.
        rc = page.insert_textbox(pymupdf.Rect(40, 40, 560, 810), text, fontsize=fontsize)
        assert rc >= 0, f"test text does not fit on one page (overflow {rc:.0f}); shorten it or reduce fontsize"
    if title:
        doc.set_metadata({"title": title})
    data = doc.tobytes()
    doc.close()
    return data


def blank_pdf(page_count: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(page_count):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def image_only_pdf(page_count: int = 2) -> bytes:
    """Simulates a scanned document: every page is a picture, no text layer."""
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 400), False)
    pix.clear_with(180)
    for _ in range(page_count):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def encrypted_pdf(user_password: str = "secret") -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Top secret content")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user_password, owner_pw="owner-" + user_password)
    doc.close()
    return data


def corrupt_pdf() -> bytes:
    """Has a valid PDF header, but the rest is garbage."""
    return b"%PDF-1.7\n" + b"\x00garbage-not-a-real-pdf-structure\xff" * 50
