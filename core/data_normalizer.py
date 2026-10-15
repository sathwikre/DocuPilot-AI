"""Schema normalisation and type inference for reconstructed tables.

* Header display names + semantic roles (core.header_normalizer).
* Summary rows ("Subtotal", "Opening balance") are classified but kept in place.
* Column types: integer / number / percent / date / text, by >= 80% agreement of non-empty cells.
* Cells that do not fit their column type keep their original text and get a review note.
* OCR letter/digit repairs (e.g. 'l,2O0.00' -> '1,200.00') are applied only in numeric columns of
  OCR-extracted rows, and every repair is written to the audit trail.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Callable, Optional

from core.header_normalizer import normalise_headers, refine_roles_by_values
from core.models import LogicalTable
from core.value_parser import infer_date_order, ocr_repair_number, parse_date, parse_number

_SUMMARY = re.compile(
    r"^\s*(sub[\s-]?total|grand\s+total|total(s)?(\s+(amount|due|debits?|credits?|for\s+.*))?|"
    r"opening\s+balance|closing\s+balance|balance\s+(b/?f|c/?f|brought\s+forward|carried\s+forward)|"
    r"brought\s+forward|carried\s+forward)\s*:?\s*$",
    re.I,
)


_ID_ROLES = {"reference", "sku", "hsn", "invoice_no"}
# Columns whose *header* already says they hold amounts need only a numeric majority to be typed as
# numbers; their unreadable cells are then flagged instead of the whole column being treated as text.
_MONEY_ROLES = {"debit", "credit", "balance", "amount", "unit_price", "net_amount", "tax_amount", "line_total",
                "quantity", "paid", "outstanding"}
_NUMERIC_LIKE = re.compile(r"[\(\-]?\s*[$€£₹]?\s*\d[\d.,'\s]*\d\s*\)?\s*%?")


def _is_summary_row(row: list[str]) -> Optional[str]:
    texts = [c for c in row if c and parse_number(c) is None and parse_date(c) is None]
    if not texts:
        return None
    for t in texts:
        if _SUMMARY.match(t):
            filled = sum(bool(c) for c in row)
            if filled <= max(3, len(row) // 2):
                return t
    return None


def type_table(t: LogicalTable, llm_mapper: Optional[Callable] = None) -> None:
    """Populate columns, roles, row kinds, col_types, values and cell_notes in place."""
    display, roles, ops = normalise_headers(t.original_header, llm_mapper=llm_mapper)
    t.columns, t.roles, t.header_ops = display, roles, ops

    for i, row in enumerate(t.rows):
        label = _is_summary_row(row)
        if label:
            t.row_meta[i].kind = "summary"
            t.row_meta[i].ops.append(f"classified as summary row ('{label}'); excluded from line-item checks")

    data_idx = t.data_row_indices()
    ncols = t.ncols
    col_types: list[str] = []
    for ci in range(ncols):
        vals = [t.rows[r][ci] for r in data_idx if t.rows[r][ci]]
        if not vals:
            col_types.append("empty")
            continue
        # Identifiers stay text even when they look numeric (keeps leading zeros, no thousands formatting).
        if t.roles[ci] in _ID_ROLES or any(re.fullmatch(r"0\d+", v) for v in vals):
            col_types.append("text")
            if any(parse_number(v) for v in vals):
                t.header_ops.append(f"'{t.columns[ci]}' treated as an identifier and kept as text")
            continue
        nums = [parse_number(v) for v in vals]
        ok_num = [n for n in nums if n is not None]
        # OCR repairs count towards the type decision, so a column is not misclassified by a few misreads.
        repaired = sum(1 for v, n in zip(vals, nums) if n is None and ocr_repair_number(v))
        order = infer_date_order(vals)
        dates = [parse_date(v, order) for v in vals]
        ok_date = [d for d in dates if d is not None]
        n = len(vals)
        # Cells that look numeric but do not parse (e.g. OCR-garbled separators) still count towards a
        # numeric column, so a few misreads cannot silently turn a money column into text.
        numeric_like = sum(1 for v, p in zip(vals, nums) if p is None and _NUMERIC_LIKE.fullmatch(v))
        if len(ok_date) / n >= 0.8:
            col_types.append("date")
        elif (len(ok_num) + max(repaired, numeric_like)) / n >= (0.5 if t.roles[ci] in _MONEY_ROLES else 0.8) and ok_num:
            if ok_num and all(x.is_percent for x in ok_num):
                col_types.append("percent")
            elif ok_num and all(x.decimals == 0 for x in ok_num) and not any("." in v for v in vals):
                col_types.append("integer")
            else:
                col_types.append("number")
        else:
            col_types.append("text")
    t.col_types = col_types
    t.header_ops.extend(refine_roles_by_values(t.roles, col_types))

    orders = {ci: infer_date_order([t.rows[r][ci] for r in data_idx]) for ci in range(ncols) if col_types[ci] == "date"}
    values: list[list[Any]] = []
    notes: dict[tuple[int, int], list[str]] = {}
    for ri, row in enumerate(t.rows):
        out: list[Any] = []
        meta = t.row_meta[ri]
        for ci, raw in enumerate(row):
            ctype = col_types[ci]
            if not raw:
                out.append(None)
                continue
            if ctype in ("number", "integer", "percent"):
                p = parse_number(raw)
                if p is None and meta.method.startswith("ocr"):
                    rep = ocr_repair_number(raw)
                    if rep:
                        p = rep[0]
                        meta.ops.append(f"OCR repair in '{t.columns[ci]}': '{raw}' -> '{rep[1]}'")
                if p is None:
                    out.append(raw)
                    if meta.kind == "data":
                        notes.setdefault((ri, ci), []).append(f"Expected a number in '{t.columns[ci]}', found text")
                    continue
                v: Decimal = p.value
                if p.is_percent:
                    v = v / Decimal(100)
                if p.drcr:
                    meta.ops.append(f"'{raw}' carries a {p.drcr} marker; sign left as printed")
                out.append(v)
            elif ctype == "date":
                d = parse_date(raw, orders.get(ci))
                if d is None:
                    out.append(raw)
                    if meta.kind == "data":
                        notes.setdefault((ri, ci), []).append(f"Expected a date in '{t.columns[ci]}', found '{raw}'")
                else:
                    out.append(d)
            else:
                out.append(raw)
        values.append(out)
    t.values = values
    t.cell_notes = notes
    for ci, ctype in enumerate(col_types):
        if ctype == "date" and orders.get(ci) is None:
            sample = [t.rows[r][ci] for r in data_idx if t.rows[r][ci]]
            if any(re.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$", s) for s in sample):
                t.warnings.append(f"Column '{t.columns[ci]}': day/month order could not be proven from the data; "
                                  "dates were read as day-first. Verify against the source.")
