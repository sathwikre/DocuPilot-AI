"""Geometry-based table detection and reconstruction from positioned words.

The same algorithm serves borderless digital tables (pdfplumber words) and scanned pages (OCR
words converted to PDF points), so behaviour is consistent across extraction paths:

1. Group words into text lines by vertical overlap.
2. Split each line into *segments* at horizontal gaps wider than ~0.8x the text height.
3. Lines with >= 3 segments are "tabular"; vertically contiguous runs of them form table blocks.
4. Column separators come from a horizontal occupancy profile of the block's data segments,
   refined with header segments so fully empty columns are not lost.
5. Words are assigned to columns; wrapped continuation lines are folded into the previous row
   (and recorded as an operation for the audit trail).
"""
from __future__ import annotations

import difflib
import re
from statistics import median
from typing import Iterable, Optional, Sequence

import numpy as np

from core.models import BBox, RawTable, Word
from core.value_parser import cell_kind, normalise_text

MIN_COLS = 3


# --------------------------------------------------------------------------- lines & segments

def group_lines(words: Iterable[Word]) -> list[list[Word]]:
    """Group words into visual lines (top-to-bottom), each sorted left-to-right."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.yc, w.x0)):
        if lines:
            last = lines[-1]
            ref_yc = median(x.yc for x in last)
            ref_h = median(x.h for x in last)
            if abs(w.yc - ref_yc) <= 0.5 * max(min(w.h, ref_h), 1.0):
                last.append(w)
                continue
        lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w.x0)
    return lines


def split_segments(line: Sequence[Word], gap_factor: float = 0.8) -> list[list[Word]]:
    """Split a line into segments separated by gaps wider than gap_factor * text height."""
    if not line:
        return []
    h = median(w.h for w in line)
    thr = max(gap_factor * h, 2.5)
    segs = [[line[0]]]
    for prev, cur in zip(line, line[1:]):
        if cur.x0 - prev.x1 > thr:
            segs.append([cur])
        else:
            segs[-1].append(cur)
    return segs


def words_text(ws: Iterable[Word]) -> str:
    return " ".join(w.text for w in sorted(ws, key=lambda w: w.x0)).strip()


def line_bbox(ws: Sequence[Word]) -> BBox:
    return (min(w.x0 for w in ws), min(w.y0 for w in ws), max(w.x1 for w in ws), max(w.y1 for w in ws))


def inside(w: Word, box: BBox, pad: float = 1.0) -> bool:
    return box[0] - pad <= w.xc <= box[2] + pad and box[1] - pad <= w.yc <= box[3] + pad


# --------------------------------------------------------------------------- header logic

_HEADER_HINT = re.compile(
    r"\b(date|description|particulars|details|amount|qty|quantity|price|rate|total|balance|debit|credit|"
    r"tax|vat|gst|sku|item|code|ref|reference|no\.?|#|unit|discount|net|region|product|category|name|id)\b",
    re.I,
)


def row_kinds(row: Sequence[str]) -> list[str]:
    return [cell_kind(c) for c in row]


def is_header_row(row: Sequence[str], following: Sequence[Sequence[str]]) -> bool:
    """Decide whether `row` is a header for the rows that follow it."""
    kinds = row_kinds(row)
    filled = [k for k in kinds if k != "empty"]
    if not filled:
        return False
    text_share = sum(k == "text" for k in filled) / len(filled)
    if text_share < 0.6:
        return False
    hint_hits = sum(bool(_HEADER_HINT.search(c or "")) for c in row)
    if hint_hits >= 2 and text_share >= 0.75:
        return True
    if not following:
        return hint_hits >= 1
    # Evidence: a column whose first cell is text while most following cells are numbers/dates.
    contrast = 0
    for ci, k in enumerate(kinds):
        if k != "text":
            continue
        col = [cell_kind(r[ci]) for r in following if ci < len(r)]
        col = [c for c in col if c != "empty"]
        if col and sum(c in ("number", "date") for c in col) / len(col) >= 0.6:
            contrast += 1
    return contrast >= 1 and text_share >= 0.75


def header_key(text: str) -> str:
    return re.sub(r"[^a-z0-9%#]+", "", (text or "").lower())


def row_matches_header(row: Sequence[str], header: Sequence[str], threshold: float = 0.85) -> bool:
    """True when a data row is (an OCR-noisy copy of) the header row."""
    if len(row) != len(header):
        return False
    pairs = [(header_key(a), header_key(b)) for a, b in zip(row, header)]
    nonempty = [(a, b) for a, b in pairs if a or b]
    if len(nonempty) < max(2, len(header) // 2):
        return False
    sims = [difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0 for a, b in nonempty]
    return sum(sims) / len(sims) >= threshold


def finalize_rows(header: Optional[list[str]], rows: list[list[str]], row_ops: list[list[str]]) -> tuple[list[list[str]], list[list[str]], int]:
    """Drop rows that repeat the header. Returns (rows, row_ops, n_removed)."""
    if not header:
        return rows, row_ops, 0
    keep_rows, keep_ops, removed = [], [], 0
    for r, ops in zip(rows, row_ops):
        if row_matches_header(r, header):
            removed += 1
            continue
        keep_rows.append(r)
        keep_ops.append(ops)
    return keep_rows, keep_ops, removed


def column_kinds(rows: Sequence[Sequence[str]], ncols: int) -> list[str]:
    """Majority lexical kind per column (ignoring empties)."""
    out = []
    for ci in range(ncols):
        ks = [cell_kind(r[ci]) for r in rows if ci < len(r)]
        ks = [k for k in ks if k != "empty"]
        if not ks:
            out.append("empty")
            continue
        best = max(("number", "date", "text"), key=ks.count)
        out.append(best if ks.count(best) / len(ks) >= 0.6 else "text")
    return out


def is_continuation_row(row: Sequence[str], col_kinds: Sequence[str], fill_ratio: Sequence[float]) -> bool:
    """A wrapped-text line: only text columns filled and every well-filled number/date column empty."""
    filled = [i for i, c in enumerate(row) if (c or "").strip()]
    if not filled:
        return False
    if any(col_kinds[i] in ("number", "date") for i in filled):
        return False
    if any(cell_kind(row[i]) in ("number", "date") for i in filled):
        return False
    anchors = [i for i, k in enumerate(col_kinds) if k in ("number", "date") and fill_ratio[i] >= 0.6]
    if not anchors:
        return False
    return len(filled) <= max(1, len(row) // 3)


def table_quality(rows: Sequence[Sequence[str]], ncols: int) -> float:
    """Heuristic 0..1 quality: fill ratio and per-column type consistency."""
    if not rows or ncols < 2:
        return 0.0
    cells = [c for r in rows for c in r]
    fill = sum(bool((c or "").strip()) for c in cells) / max(len(cells), 1)
    kinds = column_kinds(rows, ncols)
    consist = []
    for ci, k in enumerate(kinds):
        ks = [cell_kind(r[ci]) for r in rows if ci < len(r)]
        ks = [x for x in ks if x != "empty"]
        if ks:
            consist.append(sum(x == k for x in ks) / len(ks))
    cons = sum(consist) / len(consist) if consist else 0.0
    return round(0.4 * fill + 0.6 * cons, 3)


# --------------------------------------------------------------------------- grid tables

def table_from_grid(words: Sequence[Word], xs: Sequence[float], ys: Sequence[float], *, page: int,
                    page_w: float, page_h: float, method: str, ocr: bool) -> Optional[RawTable]:
    """Build a table from explicit ruling-line positions (x and y separators)."""
    xs, ys = sorted(xs), sorted(ys)
    ncols, nrows = len(xs) - 1, len(ys) - 1
    if ncols < 2 or nrows < 2:
        return None
    grid: list[list[list[Word]]] = [[[] for _ in range(ncols)] for _ in range(nrows)]
    for w in words:
        if not (xs[0] <= w.xc <= xs[-1] and ys[0] <= w.yc <= ys[-1]):
            continue
        ci = int(np.searchsorted(xs, w.xc) - 1)
        ri = int(np.searchsorted(ys, w.yc) - 1)
        if 0 <= ci < ncols and 0 <= ri < nrows:
            grid[ri][ci].append(w)
    rows: list[list[str]] = []
    for r in grid:
        cells = []
        for cell_words in r:
            # Multi-line cell: order by line then x and join with spaces.
            ordered = [w for ln in group_lines(cell_words) for w in ln]
            cells.append(normalise_text(" ".join(w.text for w in ordered)))
        rows.append(cells)
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return None
    header = rows[0] if is_header_row(rows[0], rows[1:6]) else None
    body = rows[1:] if header else rows
    ops = [[] for _ in body]
    body, ops, removed = finalize_rows(header, body, ops)
    return RawTable(
        page=page, bbox=(xs[0], ys[0], xs[-1], ys[-1]), header=header, rows=body, method=method,
        page_width=page_w, page_height=page_h, col_edges=[x / page_w for x in xs], row_ops=ops,
        raw_widths=[ncols] * len(body), quality=table_quality(body, ncols), ocr=ocr,
        repeated_headers_removed=removed,
    )


# --------------------------------------------------------------------------- whitespace tables

def _column_separators(seg_spans: list[list[tuple[float, float]]], header_spans: list[tuple[float, float]],
                       min_gap: float) -> tuple[list[float], float, float]:
    """Compute column separator x positions from per-line segment spans."""
    all_spans = [s for ln in seg_spans for s in ln] + header_spans
    x_min = min(s[0] for s in all_spans)
    x_max = max(s[1] for s in all_spans)
    size = int(np.ceil(x_max - x_min)) + 2
    occ = np.zeros(size, dtype=np.int32)
    for ln in seg_spans:
        for a, b in ln:
            occ[int(a - x_min): int(np.ceil(b - x_min)) + 1] += 1
    thr = int(0.08 * len(seg_spans))
    seps: list[float] = []
    in_gap, start = False, 0
    for x in range(size):
        free = occ[x] <= thr
        if free and not in_gap:
            in_gap, start = True, x
        elif not free and in_gap:
            in_gap = False
            if start > 0 and x - start >= min_gap:
                seps.append(x_min + (start + x) / 2)
    # Header-guided refinement: two header segments inside one column imply a missing separator,
    # unless a data segment physically spans the gap between them.
    if header_spans:
        bounds = [x_min - 1] + seps + [x_max + 1]
        extra = []
        for lo, hi in zip(bounds, bounds[1:]):
            inner = sorted(s for s in header_spans if lo <= (s[0] + s[1]) / 2 < hi)
            for (a0, a1), (b0, _b1) in zip(inner, inner[1:]):
                mid = (a1 + b0) / 2
                xi = int(mid - x_min)
                if 0 <= xi < size and occ[xi] <= thr:
                    extra.append(mid)
        seps = sorted(seps + extra)
    return seps, x_min, x_max


def _assign(segments: list[list[Word]], bounds: list[float], ncols: int) -> list[str]:
    """Assign a line's segments (or their words, if a segment straddles columns) to columns."""
    cells: list[list[str]] = [[] for _ in range(ncols)]
    for seg in segments:
        sx0, sx1 = min(w.x0 for w in seg), max(w.x1 for w in seg)
        width = max(sx1 - sx0, 0.1)
        overlaps = [max(0.0, min(sx1, hi) - max(sx0, lo)) for lo, hi in zip(bounds, bounds[1:])]
        best = int(np.argmax(overlaps))
        if overlaps[best] / width >= 0.7:
            cells[best].append(words_text(seg))
        else:
            for w in seg:
                ci = int(np.clip(np.searchsorted(bounds, w.xc) - 1, 0, ncols - 1))
                cells[ci].append(w.text)
    return [normalise_text(" ".join(c)) for c in cells]


def detect_word_tables(words: Sequence[Word], *, page: int, page_w: float, page_h: float, method: str,
                       ocr: bool, exclude: Sequence[BBox] = ()) -> list[RawTable]:
    """Detect borderless tables from positioned words."""
    pool = [w for w in words if w.text.strip() and not any(inside(w, b) for b in exclude)]
    if len(pool) < 6:
        return []
    lines = group_lines(pool)
    segs = [split_segments(ln) for ln in lines]
    tab = [len(s) >= MIN_COLS for s in segs]
    heights = [median(w.h for w in ln) for ln in lines]
    blocks: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if not tab[i]:
            i += 1
            continue
        start, last_tab, gaps = i, i, []
        k = i + 1
        while k < n:
            gap = min(w.y0 for w in lines[k]) - max(w.y1 for w in lines[k - 1])
            h = heights[k - 1]
            ref = median(gaps) if gaps else h
            if gap > max(1.8 * h, 1.9 * ref):
                break
            if tab[k]:
                gaps.append(max(gap, 0.0))
                last_tab = k
            elif k - last_tab > 2:
                break
            k += 1
        end = last_tab
        # Trailing wrapped lines (verified later as continuations).
        t = end + 1
        while t < n and t - end <= 2 and not tab[t]:
            gap = min(w.y0 for w in lines[t]) - max(w.y1 for w in lines[t - 1])
            if gap > 1.0 * heights[t - 1]:
                break
            t += 1
        if sum(tab[start:end + 1]) >= 2:
            blocks.append((start, t - 1))
        i = end + 1

    tables: list[RawTable] = []
    for start, end in blocks:
        tbl = _build_block_table(lines, segs, tab, heights, start, end, page=page, page_w=page_w,
                                 page_h=page_h, method=method, ocr=ocr)
        if tbl is not None:
            tables.append(tbl)
    return tables


def _build_block_table(lines, segs, tab, heights, start, end, *, page, page_w, page_h, method, ocr) -> Optional[RawTable]:
    block_idx = list(range(start, end + 1))
    tab_idx = [i for i in block_idx if tab[i]]
    proj = tab_idx[1:] if len(tab_idx) >= 3 else tab_idx
    seg_spans = [[(min(w.x0 for w in s), max(w.x1 for w in s)) for s in segs[i]] for i in proj]
    header_spans = [(min(w.x0 for w in s), max(w.x1 for w in s)) for s in segs[tab_idx[0]]]
    min_gap = 0.5 * median(heights[i] for i in tab_idx)
    seps, x_min, x_max = _column_separators(seg_spans, header_spans, min_gap)
    bounds = [x_min - 1] + seps + [x_max + 1]
    ncols = len(bounds) - 1
    if ncols < MIN_COLS:
        return None
    line_cells = {i: _assign(segs[i], bounds, ncols) for i in block_idx}

    # Header lines: first tabular line if it looks like a header, plus up to 2 following
    # tightly-spaced non-data lines (multi-line headers).
    first = tab_idx[0]
    lead = [i for i in block_idx if i < first]
    following = [line_cells[i] for i in tab_idx[1:8]]
    header: Optional[list[str]] = None
    body_start = first
    if is_header_row(line_cells[first], following):
        header_lines = [first]
        data_kinds = column_kinds([line_cells[i] for i in tab_idx[1:]], ncols)
        yc = {i: median(w.yc for w in lines[i]) for i in block_idx}
        data_pitches = [yc[b] - yc[a] for a, b in zip(tab_idx[1:], tab_idx[2:]) if b == a + 1]
        data_pitch = median(data_pitches) if data_pitches else 1.6 * heights[first]
        j = first + 1
        while j <= end and len(header_lines) < 3:
            cells = line_cells[j]
            # Centre-to-centre pitch is robust to OCR boxes that are much taller than the glyphs.
            pitch = yc[j] - yc[j - 1]
            if pitch > 0.85 * data_pitch or any(cell_kind(c) in ("number", "date") for c in cells if c):
                break
            filled = [ci for ci, c in enumerate(cells) if c]
            # A wrapped header line labels numeric columns or spans much of the header; a line that
            # only fills text columns is more likely a data row continued from the previous page.
            over_numeric = any(data_kinds[ci] in ("number", "date") for ci in filled)
            if not (over_numeric or len(filled) >= ncols / 2 or pitch <= 0.7 * data_pitch):
                break
            header_lines.append(j)
            j += 1
        header = [normalise_text(" ".join(line_cells[h][ci] for h in header_lines)) for ci in range(ncols)]
        body_start = header_lines[-1] + 1
    body_idx = [i for i in block_idx if i >= body_start]
    del lead

    rows: list[list[str]] = []
    ops: list[list[str]] = []
    tab_rows = [line_cells[i] for i in body_idx if tab[i]]
    kinds = column_kinds(tab_rows, ncols)
    fill = [sum(bool(r[c]) for r in tab_rows) / max(len(tab_rows), 1) for c in range(ncols)]
    last_kept = None
    for i in body_idx:
        cells = line_cells[i]
        if rows and last_kept is not None and is_continuation_row(cells, kinds, fill):
            gap = min(w.y0 for w in lines[i]) - max(w.y1 for w in lines[last_kept])
            if gap <= 1.2 * heights[i]:
                for ci, c in enumerate(cells):
                    if c:
                        rows[-1][ci] = normalise_text(f"{rows[-1][ci]} {c}")
                ops[-1].append("joined wrapped text line into row")
                last_kept = i
                continue
        if not tab[i] and i > (tab_idx[-1] if tab_idx else i):
            break  # trailing non-tabular line that is not a continuation: not part of the table
        rows.append(list(cells))
        ops.append([])
        last_kept = i
    if len(rows) < 1 or (header is None and len(rows) < 2):
        return None
    used = [i for i in block_idx if i <= (last_kept if last_kept is not None else end)]
    all_words = [w for i in used for w in lines[i]]
    bbox = line_bbox(all_words)
    rows, ops, removed = finalize_rows(header, rows, ops)
    if not rows:
        return None
    return RawTable(
        page=page, bbox=bbox, header=header, rows=rows, method=method, page_width=page_w, page_height=page_h,
        col_edges=[b / page_w for b in bounds], row_ops=ops, raw_widths=[len(segs[i]) for i in body_idx if tab[i]],
        quality=table_quality(rows, ncols), ocr=ocr, repeated_headers_removed=removed,
    )


# --------------------------------------------------------------------------- titles & free text

_KV_LINE = re.compile(r":\s*[\(\-]?[$€£₹]?\s*\d")


def find_title(lines: Sequence[Sequence[Word]], bbox: BBox, max_gap_factor: float = 3.5) -> Optional[str]:
    """Nearest short text line directly above a table bbox."""
    best, best_gap = None, None
    for ln in lines:
        lb = line_bbox(ln)
        if lb[3] > bbox[1] + 1:
            continue
        gap = bbox[1] - lb[3]
        h = median(w.h for w in ln)
        if gap > max_gap_factor * h + 4:
            continue
        if lb[2] < bbox[0] - 5 or lb[0] > bbox[2] + 5:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = ln, gap
    if best is None:
        return None
    text = words_text(best)
    # Key/value lines ("Opening Balance: 12,500.00") and multi-segment lines are not titles.
    if len(split_segments(best)) >= 2 or len(text) > 120 or _KV_LINE.search(text):
        return None
    return text


def text_lines_outside(words: Sequence[Word], boxes: Sequence[BBox]) -> list[list[Word]]:
    """Lines of words that are not inside any table bbox (used for titles and key/value fields)."""
    return group_lines([w for w in words if not any(inside(w, b, pad=2) for b in boxes)])
