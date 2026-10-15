"""Accuracy evaluation against ground truth (samples/expected/*.json).

Metrics (per sample and overall):
  cell_accuracy        exact match of every expected cell against the value exported to Excel (typed numbers/dates
                       after audited OCR repairs, raw text otherwise), after whitespace normalisation; numeric and
                       date cells also match when they parse to the same value (e.g. '1,234.50' == '1234.5').
  row_completeness     expected rows that were aligned to an extracted row / expected rows.
  column_alignment     share of expected columns whose cells match in >= 95% of aligned rows.
  duplicate_rate       extracted rows that are unwanted copies of another extracted row / extracted rows.
  extra_rows           extracted rows that align to no expected row.
  validation_recall    expected (deliberately introduced) discrepancies that were reported as critical.
  unexpected_critical  critical findings that are not in the expected list (false alarms).
  seconds              wall-clock processing time.

Usage:  python scripts/evaluate.py            (writes reports/evaluation_report.{json,md})
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_orchestrator import process_document  # noqa: E402
from core.header_normalizer import header_similarity  # noqa: E402
from core.models import LogicalTable, ProcessingResult  # noqa: E402
from core.value_parser import normalise_text, parse_date, parse_number  # noqa: E402

SAMPLES = ROOT / "samples"
EXPECTED = SAMPLES / "expected"

CASES = [
    ("sample_a_invoice.pdf", "sample_a_invoice.json"),
    ("sample_b_statement_scanned.pdf", "sample_b_statement_scanned.json"),
    ("sample_c_report.pdf", "sample_c_report.json"),
    ("sample_d_invoice_photo.png", "sample_a_invoice.json"),
]


def cells_equal(expected: str, got: str) -> bool:
    a, b = normalise_text(expected), normalise_text(got)
    if a == b:
        return True
    if not a or not b:
        return False
    pa, pb = parse_number(a), parse_number(b)
    if pa is not None and pb is not None:
        return pa.value == pb.value and pa.is_percent == pb.is_percent
    da, db = parse_date(a), parse_date(b)
    return da is not None and da == db


def row_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    n = max(len(a), len(b), 1)
    return sum(cells_equal(x, y) for x, y in zip(a, b)) / n


def align_rows(expected: list[list[str]], got: list[list[str]], threshold: float = 0.5) -> list[tuple[int, int]]:
    """Order-preserving alignment (DP) maximising total row similarity; pairs below threshold are dropped."""
    n, m = len(expected), len(got)
    sim = [[row_similarity(expected[i], got[j]) for j in range(m)] for i in range(n)]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = sim[i][j] + dp[i + 1][j + 1] if sim[i][j] >= threshold else -1
            dp[i][j] = max(take, dp[i + 1][j], dp[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if sim[i][j] >= threshold and abs(dp[i][j] - (sim[i][j] + dp[i + 1][j + 1])) < 1e-9:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def exported_rows(t: LogicalTable) -> list[list[str]]:
    """Cell strings as exported to Excel: typed numbers/dates (after audited OCR repairs), raw text otherwise."""
    out = []
    for raw_row, vals in zip(t.rows, t.values or t.rows):
        row = []
        for ci, (raw, v) in enumerate(zip(raw_row, vals)):
            ctype = t.col_types[ci] if ci < len(t.col_types) else "text"
            if isinstance(v, Decimal):
                row.append(f"{v * 100}%" if ctype == "percent" else str(v))
            elif isinstance(v, date):
                row.append(v.isoformat())
            else:
                row.append(raw)
        out.append(row)
    return out


def match_table(exp: dict, tables: Sequence[LogicalTable], used: set[str]) -> Optional[LogicalTable]:
    best, best_s = None, 0.0
    for t in tables:
        if t.table_id in used:
            continue
        s = header_similarity(exp["header"], t.original_header)
        if len(t.original_header) != len(exp["header"]):
            s *= 0.5
        if s > best_s:
            best, best_s = t, s
    return best if best_s >= 0.5 else None


def evaluate_result(result: ProcessingResult, expected: dict) -> dict[str, Any]:
    used: set[str] = set()
    totals = {"cells": 0, "cells_ok": 0, "rows": 0, "rows_aligned": 0, "cols": 0, "cols_aligned": 0,
              "extracted_rows": 0, "dup_rows": 0, "extra_rows": 0}
    per_table = []
    row_maps: dict[int, tuple[LogicalTable, dict[int, int]]] = {}
    for ti, exp in enumerate(expected["tables"]):
        t = match_table(exp, result.tables, used)
        n_rows, n_cols = len(exp["rows"]), len(exp["header"])
        totals["rows"] += n_rows
        totals["cols"] += n_cols
        totals["cells"] += n_rows * n_cols
        if t is None:
            per_table.append({"expected_table": ti, "matched": None, "row_completeness": 0.0, "cell_accuracy": 0.0})
            continue
        used.add(t.table_id)
        got = exported_rows(t)
        pairs = align_rows(exp["rows"], got)
        cell_ok = sum(sum(cells_equal(x, y) for x, y in zip(exp["rows"][i], got[j])) for i, j in pairs)
        col_ok = 0
        for c in range(n_cols):
            hits = sum(cells_equal(exp["rows"][i][c], got[j][c]) for i, j in pairs if c < len(got[j]))
            if pairs and hits / len(pairs) >= 0.95:
                col_ok += 1
        matched_j = {j for _, j in pairs}
        extra = [j for j in range(len(got)) if j not in matched_j]
        dups = [j for j in extra if any(got[j] == got[k] for k in matched_j)]
        totals["cells_ok"] += cell_ok
        totals["rows_aligned"] += len(pairs)
        totals["cols_aligned"] += col_ok
        totals["extracted_rows"] += len(got)
        totals["dup_rows"] += len(dups)
        totals["extra_rows"] += len(extra)
        row_maps[ti] = (t, {i: j for i, j in pairs})
        per_table.append({
            "expected_table": ti, "matched": t.table_id, "pages": t.pages, "expected_rows": n_rows,
            "extracted_rows": len(got), "aligned_rows": len(pairs), "row_completeness": round(len(pairs) / max(n_rows, 1), 4),
            "cell_accuracy": round(cell_ok / max(n_rows * n_cols, 1), 4),
            "column_alignment": round(col_ok / max(n_cols, 1), 4), "extra_rows": len(extra), "duplicate_rows": len(dups),
            "mismatches": [
                {"row": i + 1, "column": exp["header"][c], "expected": exp["rows"][i][c], "got": got[j][c]}
                for i, j in pairs for c in range(n_cols) if c < len(got[j]) and not cells_equal(exp["rows"][i][c], got[j][c])
            ][:25],
        })
    # Validation recall
    expected_issues = expected.get("expected_issues", [])
    found = 0
    detail = []
    matched_issue_ids = set()
    for ei in expected_issues:
        hit = None
        tmap = row_maps.get(ei["table"])
        for k, iss in enumerate(result.issues):
            if iss.check != ei["check"] or iss.severity != "critical":
                continue
            if tmap and iss.table_id != tmap[0].table_id:
                continue
            if ei.get("row") is not None:
                j = tmap[1].get(ei["row"] - 1) if tmap else None
                if j is None or iss.row_index != j:
                    continue
            if ei.get("difference") is not None and iss.difference is not None and abs(abs(iss.difference) - float(ei["difference"])) > 0.011:
                continue
            hit = k
            break
        if hit is not None:
            found += 1
            matched_issue_ids.add(hit)
        detail.append({**ei, "detected": hit is not None})
    unexpected = [
        {"check": i.check, "row": i.row_id, "explanation": i.explanation}
        for k, i in enumerate(result.issues) if i.severity == "critical" and k not in matched_issue_ids
    ]
    return {
        "cell_accuracy": round(totals["cells_ok"] / max(totals["cells"], 1), 4),
        "row_completeness": round(totals["rows_aligned"] / max(totals["rows"], 1), 4),
        "column_alignment": round(totals["cols_aligned"] / max(totals["cols"], 1), 4),
        "duplicate_rate": round(totals["dup_rows"] / max(totals["extracted_rows"], 1), 4),
        "extra_rows": totals["extra_rows"],
        "tables_expected": len(expected["tables"]), "tables_extracted": len(result.tables),
        "validation_recall": round(found / len(expected_issues), 4) if expected_issues else None,
        "validation_expected": len(expected_issues), "validation_detected": found,
        "unexpected_critical": unexpected,
        "doc_type": result.doc_type, "doc_type_expected": expected.get("doc_type"),
        "seconds": result.elapsed, "ocr_pages": len(result.ocr_pages),
        "issue_detail": detail, "tables": per_table, "_totals": totals,
    }


def run(cases=CASES) -> dict[str, Any]:
    report: dict[str, Any] = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "samples": {}}
    agg = {"cells": 0, "cells_ok": 0, "rows": 0, "rows_aligned": 0, "cols": 0, "cols_aligned": 0,
           "extracted_rows": 0, "dup_rows": 0}
    v_exp = v_found = 0
    for pdf, exp_name in cases:
        path = SAMPLES / pdf
        expected = json.loads((EXPECTED / exp_name).read_text(encoding="utf-8"))
        result = process_document(path.read_bytes(), pdf)
        out_dir = ROOT / "reports" / "sample_outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{Path(pdf).stem}_docupilot.xlsx").write_bytes(result.excel_bytes)
        ev = evaluate_result(result, expected)
        report["samples"][pdf] = ev
        for k in agg:
            agg[k] += ev["_totals"][k]
        v_exp += ev["validation_expected"]
        v_found += ev["validation_detected"]
        print(f"{pdf}: cells {ev['cell_accuracy']:.2%}, rows {ev['row_completeness']:.2%}, columns "
              f"{ev['column_alignment']:.2%}, dup {ev['duplicate_rate']:.2%}, validation "
              f"{ev['validation_detected']}/{ev['validation_expected']}, unexpected critical {len(ev['unexpected_critical'])}, "
              f"{ev['seconds']:.1f}s")
    report["overall"] = {
        "cell_accuracy": round(agg["cells_ok"] / max(agg["cells"], 1), 4),
        "row_completeness": round(agg["rows_aligned"] / max(agg["rows"], 1), 4),
        "column_alignment": round(agg["cols_aligned"] / max(agg["cols"], 1), 4),
        "duplicate_rate": round(agg["dup_rows"] / max(agg["extracted_rows"], 1), 4),
        "validation_recall": round(v_found / v_exp, 4) if v_exp else None,
        "expected_cells": agg["cells"], "expected_rows": agg["rows"],
    }
    return report


def to_markdown(report: dict[str, Any]) -> str:
    o = report["overall"]
    lines = [f"# DocuPilot AI — evaluation report", "", f"Generated {report['generated']} by `scripts/evaluate.py` "
             "against the hand-checked ground truth in `samples/expected/`.", "",
             "| Sample | Cell accuracy | Row completeness | Column alignment | Unwanted duplicates | Validation recall | Unexpected critical | Time (s) |",
             "|---|---|---|---|---|---|---|---|"]
    for name, ev in report["samples"].items():
        vr = f"{ev['validation_detected']}/{ev['validation_expected']}" if ev["validation_expected"] else "n/a (none planted)"
        lines.append(f"| {name} | {ev['cell_accuracy']:.2%} | {ev['row_completeness']:.2%} | {ev['column_alignment']:.2%} | "
                     f"{ev['duplicate_rate']:.2%} | {vr} | {len(ev['unexpected_critical'])} | {ev['seconds']:.1f} |")
    lines += ["", f"**Overall:** cell accuracy {o['cell_accuracy']:.2%} over {o['expected_cells']} expected cells; row completeness "
              f"{o['row_completeness']:.2%} over {o['expected_rows']} rows; column alignment {o['column_alignment']:.2%}; "
              f"unwanted duplicate rows {o['duplicate_rate']:.2%}; validation recall "
              f"{(o['validation_recall'] or 0):.0%}.", "", "## Cell mismatches (first 25 per table)", ""]
    for name, ev in report["samples"].items():
        for t in ev["tables"]:
            if t.get("mismatches"):
                lines.append(f"**{name} — {t['matched']}**")
                lines.append("")
                for mm in t["mismatches"]:
                    lines.append(f"- row {mm['row']}, {mm['column']}: expected `{mm['expected']}`, got `{mm['got']}`")
                lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    rep = run()
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "evaluation_report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    (out / "evaluation_report.md").write_text(to_markdown(rep), encoding="utf-8")
    print("Overall:", rep["overall"])
