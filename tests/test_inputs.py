"""Malformed, protected, oversized and edge-case inputs."""
import io

import pymupdf as fitz
import pytest
from PIL import Image

from config.settings import SETTINGS
from core.agent_orchestrator import process_document
from core.models import DocumentRejected
from scripts.generate_samples import build_long_statement
from utils.file_validation import FileValidationError, sanitize_filename, validate_upload


@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd.pdf", "passwd.pdf"),
    ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
    ("in<voice>|2024?.pdf", "in_voice_2024_.pdf"),
    ("", "document"),
    ("....pdf", "pdf"),
])
def test_filename_sanitisation_blocks_traversal(name, expected):
    assert sanitize_filename(name) == expected


def test_rejects_wrong_extension_and_mismatched_content():
    with pytest.raises(FileValidationError):
        validate_upload("payload.exe", b"MZ....")
    with pytest.raises(FileValidationError):
        validate_upload("fake.png", b"%PDF-1.7 not a png")
    with pytest.raises(FileValidationError):
        validate_upload("empty.pdf", b"")


def test_rejects_oversized_file(monkeypatch):
    big = b"%PDF-1.4\n" + b"0" * (SETTINGS.max_file_mb * 1024 * 1024 + 10)
    with pytest.raises(FileValidationError):
        validate_upload("big.pdf", big)


def test_malformed_pdf_is_rejected_gracefully():
    with pytest.raises(DocumentRejected):
        process_document(b"%PDF-1.7\n this is not really a pdf \x00\x01\x02", "broken.pdf")


def test_password_protected_pdf_is_rejected():
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(DocumentRejected, match="password"):
        process_document(data, "locked.pdf")


def test_blank_page_document_processes_without_tables():
    doc = fitz.open()
    doc.new_page()
    r = process_document(doc.tobytes(), "blank.pdf")
    assert r.tables == []
    assert r.page_reports[0].kind == "empty"
    assert any(i.check == "tables_found" and i.status == "Unable to Verify" for i in r.issues)


def test_page_without_tables_is_reported(report_result):
    page4 = report_result.page_reports[3]
    assert page4.status == "no_tables" and page4.tables_found == 0


def test_more_than_50_pages_is_rejected():
    with pytest.raises(DocumentRejected, match="limit"):
        process_document(build_long_statement(SETTINGS.max_pages + 1), "long.pdf")


def test_document_at_the_50_page_limit():
    r = process_document(build_long_statement(SETTINGS.max_pages), "fifty.pdf")
    assert r.doc_info.page_count == 50
    assert len(r.tables) == 1, "the same table continues on every page and should be stitched"
    t = r.tables[0]
    assert len(t.data_row_indices()) == 500
    assert t.pages == list(range(1, 51))
    assert len(t.repeated_headers_removed) == 49
    assert not [i for i in r.issues if i.severity == "critical"]
    assert r.elapsed < 120


def test_image_without_text_is_handled(ocr_ok):
    img = Image.new("RGB", (800, 600), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r = process_document(buf.getvalue(), "white.png")
    assert r.doc_info.source_type == "image"
    assert r.tables == []
