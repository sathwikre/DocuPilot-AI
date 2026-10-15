"""Step D: reconstruct logical tables from per-page fragments (multi-page stitching).

A fragment is appended to the previous table only when the evidence supports it. Signals:
  + header repeated on the new page (fuzzy, OCR tolerant)        + title says "continued"
  + no header but column data types match                        + column x-positions align
  + previous fragment ends low on its page, next starts near top + same title
  - different header present (hard stop)    - column count differs (hard stop)
  - different, non-"continued" title        - other content between the fragments (hard stop)
Every decision, with its evidence, is recorded for the audit trail.

At a page boundary, a first row containing only wrapped text is joined to the previous row, and
an exact repeat of the previous row is removed *only* if a running balance proves it is the same
transaction printed twice; otherwise it is kept and flagged for review.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional, Sequence

from core.header_normalizer import header_similarity, match_role
from core.models import LogicalTable, MergeDecision, RawTable, RowMeta
from core.table_detector import column_kinds, is_continuation_row
from core.value_parser import cell_kind, normalise_text

_CONTINUED = re.compile(r"\b(continued|cont'?d\.?|contd\.?)\b|\(cont", re.I)


def _norm_title(t: Optional[str]) -> str:
    return re.sub(r"\s+", " ", _CONTINUED.sub("", t or "")).strip(" -–:()").lower()


def _type_match(rows_a: Sequence[Sequence[str]], rows_b: Sequence[Sequence[str]], ncols: int) -> float:
    ka, kb = column_kinds(rows_a, ncols), column_kinds(rows_b, ncols)
    pairs = [(a, b) for a, b in zip(ka, kb) if a != "empty" and b != "empty"]
    if not pairs:
        return 0.0
    return sum(a == b for a, b in pairs) / len(pairs)


def decide_merge(group: list[RawTable], right: RawTable) -> MergeDecision:
    """Decide whether `right` continues the table formed by `group`."""
    head, left = group[0], group[-1]
    ev: list[str] = []
    blockers: list[str] = []
    score = 0.0
    if right.page != left.page + 1:
        return MergeDecision(left.ref, right.ref, False, 0, ["fragments are not on consecutive pages"])
    if not left.is_last_on_page:
        blockers.append("another table follows the earlier fragment on its page")
    if not right.is_first_on_page:
        blockers.append("another table precedes the later fragment on its page")
    ref_header = next((g.header for g in group if g.header), None)
    ncols = len(ref_header) if ref_header else left.ncols
    if right.ncols != ncols:
        blockers.append(f"column count differs ({ncols} vs {right.ncols})")
    if right.header:
        sim = header_similarity(ref_header, right.header) if ref_header else 0.0
        if sim >= 0.85:
            score += 3
            ev.append(f"header repeated on page {right.page} (similarity {sim:.2f})")
        elif sim >= 0.6:
            score += 1
            ev.append(f"header partly similar (similarity {sim:.2f})")
        else:
            blockers.append(f"different header on page {right.page} (similarity {sim:.2f}) — independent table")
    else:
        all_rows = [r for g in group for r in g.rows]
        tm = _type_match(all_rows, right.rows, ncols)
        if tm >= 0.8:
            score += 2
            ev.append(f"no header on page {right.page}; column data types match ({tm:.0%})")
        else:
            score -= 2
            ev.append(f"no header and column data types differ (match {tm:.0%})")
    group_title = next((g.title for g in group if g.title), None)
    if right.title and _CONTINUED.search(right.title):
        score += 2
        ev.append(f"title marks continuation: '{right.title}'")
    elif right.title and group_title:
        ts = difflib.SequenceMatcher(None, _norm_title(right.title), _norm_title(group_title)).ratio()
        if ts >= 0.85:
            score += 1
            ev.append("same title on both pages")
        else:
            score -= 3
            ev.append(f"new title '{right.title}' differs from '{group_title}'")
    elif right.title and not group_title and not right.header:
        score -= 1
        ev.append(f"later fragment has its own title '{right.title}'")
    if left.col_edges and right.col_edges and len(left.col_edges) == len(right.col_edges):
        diff = sum(abs(a - b) for a, b in zip(left.col_edges, right.col_edges)) / len(left.col_edges)
        if diff < 0.02:
            score += 1
            ev.append(f"column positions align (mean offset {diff:.3f} of page width)")
        elif diff > 0.06:
            score -= 1
            ev.append(f"column positions differ (mean offset {diff:.3f})")
    if left.bbox[3] > 0.55 * left.page_height and right.bbox[1] < 0.35 * right.page_height:
        score += 1
        ev.append("earlier fragment runs to the lower page area and the next starts near the top")
    merged = not blockers and score >= 3
    uncertainty = None
    if merged and score < 4:
        uncertainty = "moderate evidence; review the boundary rows"
    if not merged and not blockers and score >= 1:
        uncertainty = "weak evidence for continuation; kept as a separate table"
    return MergeDecision(left.ref, right.ref, merged, score, blockers + ev, uncertainty)


def _boundary_balance_col(header: Optional[Sequence[str]]) -> Optional[int]:
    if not header:
        return None
    for i, h in enumerate(header):
        if match_role(h)[0] == "balance":
            return i
    return None


def assemble(table_id: str, group: list[RawTable]) -> LogicalTable:
    """Concatenate fragments into one LogicalTable with provenance for every row."""
    header = next((g.header for g in group if g.header), None)
    ncols = len(header) if header else max(g.ncols for g in group)
    generated = header is None
    if header is None:
        header = [f"Column {i + 1}" for i in range(ncols)]
    rows: list[list[str]] = []
    meta: list[RowMeta] = []
    warnings: list[str] = []
    repeated: list[dict] = []
    bal_col = _boundary_balance_col(header)
    for gi, seg in enumerate(group):
        warnings.extend(f"Page {seg.page}: {w}" for w in seg.warnings)
        if seg.repeated_headers_removed:
            repeated.append({"page": seg.page, "count": seg.repeated_headers_removed, "where": "within page"})
        if gi > 0 and seg.header:
            repeated.append({"page": seg.page, "count": 1, "where": "top of page (continuation header)"})
        kinds = column_kinds(rows, ncols) if rows else []
        fill = [sum(bool(r[c]) for r in rows) / max(len(rows), 1) for c in range(ncols)] if rows else []
        for ri, (r, ops) in enumerate(zip(seg.rows, seg.row_ops or [[] for _ in seg.rows])):
            ops = list(ops)
            if len(r) != ncols:
                ops.append(f"row width {len(r)} adjusted to {ncols} columns")
                r = (list(r) + [""] * ncols)[:ncols]
            r = [normalise_text(c) for c in r]
            if gi > 0 and ri == 0 and rows:
                # Page boundary handling.
                if is_continuation_row(r, kinds, fill):
                    for ci, c in enumerate(r):
                        if c:
                            rows[-1][ci] = normalise_text(f"{rows[-1][ci]} {c}")
                    meta[-1].ops.append(f"joined continued text from top of page {seg.page} into this row")
                    continue
                if r == rows[-1]:
                    if bal_col is not None and cell_kind(r[bal_col]) == "number":
                        warnings.append(
                            f"Removed an exact repeat of the previous row at the top of page {seg.page}: identical "
                            f"running balance ({r[bal_col]}) shows it is the same transaction printed twice.")
                        meta[-1].ops.append(f"exact repeat at top of page {seg.page} removed (balance-confirmed)")
                        continue
                    ops.append("identical to previous row across a page boundary; kept (no evidence it is an extraction duplicate)")
            rows.append(r)
            meta.append(RowMeta(row_id="", page=seg.page, method=seg.method, ops=ops, segment=seg.ref))
    for i, m in enumerate(meta, start=1):
        m.row_id = f"{table_id}-R{i}"
    title = next((g.title for g in group if g.title), None)
    if title and _CONTINUED.search(title):
        title = re.sub(r"\s*\((continued|cont'?d\.?|contd\.?)\)\s*$", "", title, flags=re.I)
        title = _CONTINUED.sub("", title).strip(" -–:") or title
    return LogicalTable(
        table_id=table_id, title=title, pages=sorted({g.page for g in group}), original_header=list(header),
        columns=list(header), roles=[None] * ncols, rows=rows, row_meta=meta,
        methods=sorted({g.method for g in group}), warnings=warnings, segments=[g.ref for g in group],
        repeated_headers_removed=repeated, header_generated=generated,
    )


def build_logical_tables(raws: Sequence[RawTable], running_text: set[str] = frozenset()) -> tuple[list[LogicalTable], list[MergeDecision]]:
    """Group page fragments into logical tables. Returns (tables, merge decisions)."""
    ordered = sorted(raws, key=lambda t: (t.page, t.bbox[1], t.bbox[0]))
    for t in ordered:
        # Running page headers ("ACME BANK - STATEMENT" on every page) are not table titles.
        if t.title and normalise_text(t.title).lower() in running_text:
            t.title = None
    groups: list[list[RawTable]] = []
    decisions: list[MergeDecision] = []
    for raw in ordered:
        if groups:
            d = decide_merge(groups[-1], raw)
            if raw.page == groups[-1][-1].page + 1:
                decisions.append(d)
            if d.merged:
                groups[-1].append(raw)
                continue
        groups.append([raw])
    tables = []
    for gi, g in enumerate(groups, start=1):
        tid = f"T{gi}"
        tables.append(assemble(tid, g))
        refs = {x.ref for x in g}
        for d in decisions:
            if d.right in refs or (d.merged and d.left in refs):
                d.table_id = d.table_id or tid
    for t in tables:
        t.merge_decisions = [d for d in decisions if d.table_id == t.table_id]
    return tables, decisions
