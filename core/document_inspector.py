"""Step A: open and inspect a document; classify every page before extraction."""
from __future__ import annotations

import io
import re

import pymupdf as fitz  # PyMuPDF

from config.settings import SETTINGS
from core.models import DocumentInfo, DocumentRejected, PageInfo
from utils.logging_config import get_logger, log_event

log = get_logger("inspector")

_NUMERIC_TOKEN = re.compile(r"^[\(\-]?[$€£₹]?\d[\d,]*(\.\d+)?\)?%?$")


def open_document(data: bytes, source_type: str) -> fitz.Document:
    """Open PDF bytes, or wrap an image (incl. multi-frame TIFF) into an in-memory PDF.

    Raises DocumentRejected for malformed, encrypted or empty inputs.
    """
    try:
        if source_type == "image":
            img_doc = fitz.open(stream=data, filetype=_image_filetype(data))
            pdf_bytes = img_doc.convert_to_pdf()
            img_doc.close()
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        else:
            doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several error types for corrupt input
        raise DocumentRejected(f"The file could not be opened — it may be corrupted or not a valid document ({type(exc).__name__}).") from exc
    if doc.needs_pass:
        doc.close()
        raise DocumentRejected("The PDF is password-protected. Remove the password and upload it again.")
    if doc.page_count == 0:
        doc.close()
        raise DocumentRejected("The document contains no pages.")
    return doc


def _image_filetype(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data.startswith(b"BM"):
        return "bmp"
    if data[8:12] == b"WEBP":
        return "webp"
    return "png"


def _image_coverage(page: fitz.Page) -> float:
    """Fraction of the page area covered by raster images (union approximated by clipping)."""
    area = abs(page.rect)
    if area <= 0:
        return 0.0
    covered = 0.0
    try:
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"]) & page.rect
            if not r.is_empty:
                covered += abs(r)
    except Exception:
        return 0.0
    return min(covered / area, 1.0)


def inspect_page(page: fitz.Page) -> PageInfo:
    """Classify one page as digital / scanned / mixed / empty and estimate table likelihood."""
    text = page.get_text("text") or ""
    chars = len(re.sub(r"\s", "", text))
    garbled = text.count("�") + len(re.findall(r"\(cid:\d+\)", text)) * 5
    garbled_ratio = garbled / max(chars, 1)
    coverage = _image_coverage(page)
    try:
        drawings = page.get_drawings()
        line_count = sum(1 for d in drawings for it in d.get("items", ()) if it[0] in ("l", "re"))
    except Exception:
        line_count = 0
    notes: list[str] = []
    if chars < SETTINGS.min_text_chars and coverage > 0.05:
        kind = "scanned"
    elif chars < SETTINGS.min_text_chars:
        kind = "empty"
    elif garbled_ratio > 0.2:
        kind = "scanned"
        notes.append("Text layer appears garbled (unmapped glyphs); OCR will be used.")
    elif coverage >= SETTINGS.image_heavy_ratio:
        kind = "mixed"
        notes.append(f"Page is image-heavy ({coverage:.0%} covered by images).")
    else:
        kind = "digital"
    tokens = text.split()
    numeric_share = sum(bool(_NUMERIC_TOKEN.match(t)) for t in tokens) / max(len(tokens), 1)
    likely_table = kind in ("scanned", "mixed") or line_count >= 8 or numeric_share > 0.15
    if page.rotation:
        notes.append(f"Page has a /Rotate of {page.rotation}°; rendering applies it.")
    return PageInfo(
        number=page.number + 1,
        width=float(page.rect.width),
        height=float(page.rect.height),
        rotation=int(page.rotation),
        char_count=chars,
        image_coverage=round(coverage, 3),
        line_count=line_count,
        garbled_ratio=round(garbled_ratio, 3),
        kind=kind,
        likely_table=likely_table,
        notes=notes,
    )


def inspect_document(doc: fitz.Document, filename: str, source_type: str) -> DocumentInfo:
    """Inspect all pages; enforce the page limit."""
    if doc.page_count > SETTINGS.max_pages:
        raise DocumentRejected(
            f"The document has {doc.page_count} pages; the limit is {SETTINGS.max_pages}. Split it and upload the parts."
        )
    pages = [inspect_page(doc[i]) for i in range(doc.page_count)]
    meta = {k: v for k, v in (doc.metadata or {}).items() if v and k in ("format", "producer", "creator")}
    warnings: list[str] = []
    if all(p.kind == "empty" for p in pages):
        warnings.append("No text or images were found on any page; the document may be blank.")
    info = DocumentInfo(filename=filename, source_type=source_type, page_count=doc.page_count,
                        pages=pages, metadata=meta, warnings=warnings)
    log_event(log, "document inspected", pages=doc.page_count,
              kinds={k: sum(p.kind == k for p in pages) for k in ("digital", "scanned", "mixed", "empty")})
    return info


def pdf_bytes_for(doc: fitz.Document) -> bytes:
    """Serialise an opened document (e.g. a wrapped image) back to PDF bytes for pdfplumber."""
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
