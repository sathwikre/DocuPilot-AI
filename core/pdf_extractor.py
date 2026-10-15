"""Step B (digital pages): native text and table extraction with pdfplumber.

Primary method: ruled tables via pdfplumber's line-intersection finder.
Fallback / complement: geometric word clustering (core.table_detector) for borderless tables and
for ruled tables whose rows come out malformed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pdfplumber

from core.models import RawTable, Word
from core.table_detector import (
    detect_word_tables, finalize_rows, find_title, inside, is_header_row, table_quality, text_lines_outside,
)
from core.value_parser import cell_kind, normalise_text

_LINE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
}


@dataclass
class PageExtraction:
    """Tables and free-text lines found on one page."""

    tables: list[RawTable]
    words: list[Word]
    text_lines: list[list[Word]]
    notes: list[str] = field(default_factory=list)


def page_words(page: "pdfplumber.page.Page") -> list[Word]:
    """Words with bounding boxes in PDF points (top-left origin)."""
    out = []
    for w in page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False, use_text_flow=False):
        text = w["text"].strip()
        if text:
            out.append(Word(text, float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"])))
    return out


def _combine_header_rows(rows: list[list[str]]) -> tuple[Optional[list[str]], int]:
    """Detect a (possibly 2-row grouped) header at the top of a ruled table."""
    if len(rows) < 2 or not is_header_row(rows[0], rows[1:7]):
        return None, 0
    header = list(rows[0])
    used = 1
    # A grouped header leaves blank (spanned) cells in row 0 that row 1 fills with sub-labels.
    if len(rows) > 2 and any(not c for c in rows[0]):
        nxt = rows[1]
        if all(cell_kind(c) in ("text", "empty") for c in nxt) and any(nxt) and \
                any(cell_kind(c) in ("number", "date") for c in rows[2]):
            header = [normalise_text(f"{a} {b}") for a, b in zip(header, nxt)]
            used = 2
    # Fill spanned header blanks so every column is named.
    header = [h if h else f"Column {i + 1}" for i, h in enumerate(header)]
    return header, used


def _ruled_tables(page, page_no: int) -> list[RawTable]:
    out: list[RawTable] = []
    try:
        found = page.find_tables(_LINE_SETTINGS)
    except Exception:
        return out
    for t in found:
        try:
            data = t.extract()
        except Exception:
            continue
        rows = [[normalise_text(c) for c in r] for r in data if r]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue
        ncols = max(len(r) for r in rows)
        if ncols < 2:
            continue
        widths = [len(r) for r in rows]
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        # Drop columns that are empty in every row (artefacts of decorative vertical rules).
        keep = [ci for ci in range(ncols) if any(r[ci] for r in rows)]
        rows = [[r[ci] for ci in keep] for r in rows]
        header, used = _combine_header_rows(rows)
        body = rows[used:]
        ops = [[] for _ in body]
        body, ops, removed = finalize_rows(header, body, ops)
        xs = sorted({round(c[0], 1) for c in t.cells} | {round(c[2], 1) for c in t.cells})
        out.append(RawTable(
            page=page_no, bbox=tuple(float(v) for v in t.bbox), header=header, rows=body, method="pdf-ruled",
            page_width=float(page.width), page_height=float(page.height),
            col_edges=[x / float(page.width) for x in xs], row_ops=ops, raw_widths=widths[used:],
            quality=table_quality(body, len(keep)), repeated_headers_removed=removed,
        ))
    return out


def extract_digital_page(page, page_no: int, words: Optional[list[Word]] = None) -> PageExtraction:
    """Extract tables from one digital page, with geometric fallback for malformed/borderless tables."""
    words = page_words(page) if words is None else words
    notes: list[str] = []
    ruled = _ruled_tables(page, page_no)
    tables: list[RawTable] = []
    for t in ruled:
        if t.quality < 0.55:
            # Recovery: ruled extraction looks malformed; try geometric reconstruction inside its bbox.
            alt = detect_word_tables([w for w in words if inside(w, t.bbox)], page=page_no,
                                     page_w=float(page.width), page_h=float(page.height),
                                     method="pdf-layout", ocr=False)
            if alt and alt[0].quality > t.quality + 0.05:
                notes.append(f"Ruled table at y={t.bbox[1]:.0f} looked malformed (quality {t.quality:.2f}); "
                             f"used layout reconstruction instead (quality {alt[0].quality:.2f}).")
                alt[0].warnings.append("Recovered with layout-based reconstruction after malformed ruled extraction.")
                tables.append(alt[0])
                continue
        tables.append(t)
    borderless = detect_word_tables(words, page=page_no, page_w=float(page.width), page_h=float(page.height),
                                    method="pdf-layout", ocr=False, exclude=[t.bbox for t in tables])
    tables.extend(borderless)
    tables.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    lines = text_lines_outside(words, [t.bbox for t in tables])
    for i, t in enumerate(tables):
        t.index_on_page = i
        t.is_first_on_page = i == 0
        t.is_last_on_page = i == len(tables) - 1
        t.title = find_title(lines, t.bbox)
    return PageExtraction(tables=tables, words=words, text_lines=lines, notes=notes)


def open_plumber(pdf_bytes: bytes):
    """Open a pdfplumber document from bytes (no temporary files)."""
    import io
    return pdfplumber.open(io.BytesIO(pdf_bytes))
