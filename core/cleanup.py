"""The "common problems" checklist for moving a document into Excel.

Every entry is one thing people run into when they copy a PDF table into a spreadsheet by hand. For each entry:
  detect  how many places in THIS document have the problem (with up to three examples) and whether to suggest it;
  apply   the fix, when the box is ticked. Text and row fixes are applied here on a copy of the reconstructed tables;
          number/date fixes are typing options (core.data_normalizer.TypingOptions); 'totals_separate' and
          'flag_cells' change only how the workbook is written.
Fixes run on the stored, already-reconstructed tables, so ticking a box rebuilds the workbook without reading the
document again. Every edit is written to the row's audit trail and every removed row is kept on a 'Removed rows'
sheet - nothing disappears silently.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from core.data_normalizer import TypingOptions, is_identifier_column, is_summary_row
from core.models import LogicalTable, RemovedRow, ValidationIssue
from core.value_parser import (
    clean_spaces, infer_date_order, ocr_repair_number, parse_date, parse_number,
)


@dataclass(frozen=True)
class Option:
    id: str
    section: str
    label: str
    help: str
    default: bool = False


SECTIONS = ("Numbers", "Dates", "Text", "Rows and totals", "Workbook")

OPTIONS: tuple[Option, ...] = (
    Option("numbers", "Numbers", "Numbers saved as text (they cannot be added up)",
           "Turns 1,234.50, (500), 12% or ₹ 1,200 into real numbers with a matching number format, so SUM and "
           "sorting work.", True),
    Option("leading_zeros", "Numbers", "Leading zeros lost in IDs, PIN codes, phone and account numbers",
           "Keeps columns like 000123, invoice numbers or SKUs as text so Excel does not strip or reformat them.", True),
    Option("ocr_digits", "Numbers", "Letters mistaken for digits by OCR (O for 0, l for 1, S for 5, B for 8)",
           "Inside number columns of scanned pages only: a cell that becomes a number after swapping look-alike "
           "letters is corrected, and the repair is logged.", True),
    Option("drcr", "Numbers", "Balances written with Dr / Cr",
           "Makes Dr amounts negative and Cr amounts positive. Left unticked, the sign stays as printed."),
    Option("decimal_comma", "Numbers", "European numbers like 1.234,56",
           "Reads the comma as the decimal point and the dot as the thousands separator."),
    Option("zero_blanks", "Numbers", "Empty cells inside number columns",
           "Writes 0 in blank number cells of data rows (some formulas and charts need a value there)."),
    Option("dates", "Dates", "Dates saved as text",
           "Turns 05/03/2026, 5-Mar-26 or March 5, 2026 into real Excel dates.", True),
    Option("month_first", "Dates", "Dates written month/day (US style)",
           "For dates like 03/04/2026 that could be read either way: reads them as March 4 instead of 3 April."),
    Option("trim_spaces", "Text", "Extra spaces, line breaks and hidden characters",
           "Removes leading, trailing and double spaces, non-breaking spaces and invisible characters.", True),
    Option("hyphen_breaks", "Text", "Words split with a hyphen at the end of a line",
           "Joins 'manufac- turing' back into 'manufacturing'."),
    Option("case", "Text", "ALL-CAPITAL text",
           "Changes SHARMA TRADERS to Sharma Traders in text columns that are mostly capitals (abbreviations "
           "like GST or UPI and codes with digits stay as they are)."),
    Option("fill_down", "Rows and totals", "Group name only on the first row (looks like merged cells)",
           "Copies the group name into the blank cells below it so every row can be filtered and sorted on its own."),
    Option("duplicates", "Rows and totals", "Repeated (duplicate) rows",
           "Removes a row that is exactly the same as an earlier one. Removed rows are listed on the "
           "'Removed rows' sheet."),
    Option("totals_separate", "Rows and totals", "Total and sub-total rows mixed in with the data",
           "Moves them to a separate 'Totals' sheet so a SUM over the data is not counted twice. The checks still "
           "use them."),
    Option("drop_empty", "Rows and totals", "Blank rows and completely empty columns",
           "Deletes rows with nothing in them and columns with no data.", True),
    Option("flag_cells", "Workbook", "Highlight cells that need a human check",
           "Colours cells with a failed total, balance or type check and adds a comment saying why.", True),
)
OPTION_IDS = tuple(o.id for o in OPTIONS)
OPTIONS_BY_ID = {o.id: o for o in OPTIONS}
DEFAULT_SELECTION = frozenset(o.id for o in OPTIONS if o.default)


def default_selection(remove_repeats: bool) -> frozenset[str]:
    """The boxes ticked for a new conversion. The upload form's 'remove repeated rows?' answer ticks 'duplicates'."""
    return DEFAULT_SELECTION | ({"duplicates"} if remove_repeats else set())


def typing_options(selected: Iterable[str]) -> TypingOptions:
    s = set(selected)
    return TypingOptions(numbers="numbers" in s, dates="dates" in s, leading_zeros="leading_zeros" in s,
                         ocr_digits="ocr_digits" in s, drcr_sign="drcr" in s, decimal_comma="decimal_comma" in s,
                         month_first="month_first" in s, zero_blanks="zero_blanks" in s)


# ----------------------------------------------------------------------------------------------- recognisers
_HYPHEN_BREAK = re.compile(r"([a-z])- ([a-z])")
_AMBIGUOUS_DATE = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})\s*$")
_KEEP_UPPER = {
    "GST", "GSTIN", "PAN", "UPI", "IFSC", "NEFT", "RTGS", "IMPS", "ATM", "LLC", "LTD", "PVT", "INC", "USA", "UK",
    "UAE", "ID", "PIN", "SKU", "HSN", "VAT", "TDS", "EMI", "CGST", "SGST", "IGST", "PDF", "OCR", "CEO", "CFO",
    "LLP", "KG", "ML", "MG", "USD", "EUR", "GBP", "INR", "PO", "NA", "N/A", "QTY", "EA", "PCS", "CR", "DR",
}


def smart_title(text: str) -> str:
    """Title Case for SHOUTING text, leaving abbreviations (GST, UPI...) and anything with digits alone."""

    def fix(m: re.Match) -> str:
        run = m.group(0)
        if any(ch.isdigit() for ch in run) or run in _KEEP_UPPER or not run.isupper():
            return run
        return run.title()

    return re.sub(r"[A-Za-z0-9]+", fix, text)


def is_shouting(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 4 and all(ch.isupper() for ch in letters)


def _filled(row: Sequence[str]) -> int:
    return sum(1 for c in row if c and c.strip())


def row_key(row: Sequence[str]) -> tuple[str, ...]:
    return tuple(clean_spaces(c).lower() for c in row)


def fill_plan(t: LogicalTable, types: Sequence[str]) -> list[tuple[int, int]]:
    """(row, column) of blank cells that only look blank because a group name was written once above them."""
    plan: list[tuple[int, int]] = []
    ncols = len(t.rows[0]) if t.rows else 0
    data = [r for r, m in enumerate(t.row_meta) if m.kind == "data"]
    if len(data) < 3:
        return plan
    for c in range(min(ncols, 3)):
        if c >= len(types) or types[c] != "text":
            continue
        values = [t.rows[r][c].strip() for r in data]
        blanks = [i for i, v in enumerate(values) if not v]
        if len(blanks) < 0.3 * len(values) or len(blanks) == len(values) or not values[0]:
            continue  # a few blanks are real blanks; a group name written once above its rows leaves many
        named = [v for v in values if v]
        if len(set(named)) / len(named) < 0.7:
            continue  # a column that already repeats its values is filled in, not a merged-cell group
        for i in blanks:
            r = data[i]
            others = sum(1 for k, cell in enumerate(t.rows[r]) if k != c and cell.strip())
            if others >= max(1, 0.5 * (ncols - 1)):
                plan.append((r, c))
    return plan


# ----------------------------------------------------------------------------------------------- detection
def detect(tables: Sequence[LogicalTable], issues: Sequence[ValidationIssue] = ()) -> dict[str, dict]:
    """For every checklist entry: how many places in this document have the problem, whether to suggest the fix,
    and up to three examples. `tables` are the typed tables from the default conversion."""
    count = {o.id: 0 for o in OPTIONS}
    examples: dict[str, list[str]] = {o.id: [] for o in OPTIONS}

    def hit(option: str, example: str = "") -> None:
        count[option] += 1
        if example and len(examples[option]) < 3 and example not in examples[option]:
            examples[option].append(example[:60])

    comma_dec = dot_dec = upper_cells = text_cells = 0
    for t in tables:
        data = [r for r, m in enumerate(t.row_meta) if m.kind == "data"]
        for c, kind in enumerate(t.col_types):
            raw = [t.rows[r][c] for r in data]
            vals = [v for v in raw if v]
            if not vals:
                continue
            numeric = [v for v in vals if parse_number(v) is not None]
            if kind == "text" and numeric and is_identifier_column(t.roles[c], vals):
                for v in numeric:
                    hit("leading_zeros", v if v.startswith("0") or len(v) >= 6 else "")
            elif kind in ("number", "integer", "percent"):
                for v in vals:
                    p = parse_number(v)
                    if p is not None:
                        hit("numbers", v if (p.currency or p.is_percent or v.startswith("(") or "," in v) else "")
                        if p.drcr:
                            hit("drcr", v)
                        if re.search(r"\d,\d{1,2}\)?$", v):
                            comma_dec += 1  # 1.234,56 / 15,25
                        elif re.search(r"\d\.\d{1,2}\)?$", v):
                            dot_dec += 1  # 1,234.56 / 15.25
                    else:
                        rep = ocr_repair_number(v)
                        if rep:
                            hit("ocr_digits", f"{v} → {rep[1]}")
                blanks = sum(1 for v in raw if not v)
                if 0 < blanks <= 0.25 * len(raw):
                    for _ in range(blanks):
                        hit("zero_blanks")
            elif kind == "date":
                order = infer_date_order(vals)
                for v in vals:
                    if parse_date(v, order) is not None:
                        hit("dates", v)
                        m = _AMBIGUOUS_DATE.match(v)
                        if order is None and m and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 \
                                and m.group(1) != m.group(2):
                            hit("month_first", v)
            elif kind == "text":
                for v in vals:
                    if any(ch.isalpha() for ch in v):
                        text_cells += 1
                        if is_shouting(v):
                            upper_cells += 1
                            if len(examples["case"]) < 3:
                                examples["case"].append(f"{v[:28]} → {smart_title(v)[:28]}")
        seen: set[tuple[str, ...]] = set()
        for r, row in enumerate(t.rows):
            if any(cell != clean_spaces(cell) for cell in row):
                hit("trim_spaces", next((repr(cell)[1:-1] for cell in row if cell != clean_spaces(cell)), ""))
            for cell in row:
                m = _HYPHEN_BREAK.search(cell)
                if m:
                    hit("hyphen_breaks", cell[max(0, m.start() - 12):m.end() + 12])
            if not _filled(row):
                hit("drop_empty")
            if t.row_meta[r].kind == "summary":
                hit("totals_separate", next((c for c in row if c.strip()), ""))
            if _filled(row) >= 2:
                key = row_key(row)
                if key in seen:
                    hit("duplicates", " | ".join(c for c in row if c.strip()))
                seen.add(key)
        for r, c in fill_plan(t, t.col_types):
            hit("fill_down", t.rows[r][c] or next((t.rows[k][c] for k in range(r, -1, -1) if t.rows[k][c]), ""))
        for c, kind in enumerate(t.col_types):
            if kind == "empty" and not any(row[c].strip() for row in t.rows):
                hit("drop_empty", f"empty column '{t.columns[c]}'")
    count["case"] = upper_cells
    count["flag_cells"] = sum(1 for i in issues if i.severity in ("critical", "warning") and i.row_index is not None)

    suggested = {
        "ocr_digits": count["ocr_digits"] > 0,
        "drcr": count["drcr"] > 0,
        "decimal_comma": comma_dec >= 2 and dot_dec == 0,
        "fill_down": count["fill_down"] > 0,
        "duplicates": count["duplicates"] > 0,
        "totals_separate": count["totals_separate"] > 0,
        "hyphen_breaks": count["hyphen_breaks"] > 0,
        "case": text_cells >= 4 and upper_cells >= 0.6 * text_cells,
        "month_first": False,
    }
    count["decimal_comma"] = comma_dec if suggested["decimal_comma"] else 0
    if not suggested["decimal_comma"]:
        examples["decimal_comma"] = []
    return {o.id: {"count": count[o.id], "suggested": bool(suggested.get(o.id)), "examples": examples[o.id]}
            for o in OPTIONS}


# ----------------------------------------------------------------------------------------------- fixing
@dataclass
class Cleaned:
    tables: list[LogicalTable]
    removed: list[RemovedRow] = field(default_factory=list)
    edits: int = 0


def _drop_column(t: LogicalTable, c: int) -> None:
    t.original_header.pop(c)
    for row in t.rows:
        row.pop(c)


def clean(base: Sequence[LogicalTable], selected: Iterable[str],
          ref_types: Optional[dict[str, list[str]]] = None) -> Cleaned:
    """Apply the ticked text and row fixes to a copy of the reconstructed (untyped) tables.

    ref_types maps table_id -> column types from the default conversion; text-only fixes (case, fill-down) use it to
    stay out of number and date columns.
    """
    sel = set(selected)
    out = Cleaned([copy.deepcopy(t) for t in base])
    for t in out.tables:
        types = (ref_types or {}).get(t.table_id, [])
        for r, row in enumerate(t.rows):  # mark totals first so row fixes leave them alone
            if is_summary_row(row):
                t.row_meta[r].kind = "summary"
        for r, row in enumerate(t.rows):
            meta = t.row_meta[r]
            for c, cell in enumerate(row):
                new = cell
                if "trim_spaces" in sel:
                    new = clean_spaces(new)
                if "hyphen_breaks" in sel and _HYPHEN_BREAK.search(new):
                    new = _HYPHEN_BREAK.sub(r"\1\2", new)
                if "case" in sel and c < len(types) and types[c] == "text" and is_shouting(new):
                    new = smart_title(new)
                if new != cell:
                    out.edits += 1
                    if clean_spaces(new) != clean_spaces(cell):  # whitespace-only edits are too noisy to log
                        meta.ops.append(f"clean-up: '{cell.strip()}' -> '{new}'")
                    row[c] = new

        if "fill_down" in sel:
            for r, c in fill_plan(t, types):
                above = next((t.rows[k][c] for k in range(r - 1, -1, -1) if t.rows[k][c].strip()), "")
                if above:
                    t.rows[r][c] = above
                    t.row_meta[r].ops.append(f"clean-up: group name '{above}' filled into the blank cell")
                    out.edits += 1

        if "drop_empty" in sel and t.rows:
            for c in range(len(t.original_header) - 1, -1, -1):
                blank_header = not t.original_header[c].strip() or bool(_GENERATED.fullmatch(t.original_header[c]))
                if len(t.original_header) > 1 and blank_header and not any(row[c].strip() for row in t.rows):
                    _drop_column(t, c)
            _remove(t, out, lambda r: None if _filled(t.rows[r]) else "Blank row")

        if "duplicates" in sel:
            seen: dict[tuple[str, ...], tuple[str, int]] = {}

            def repeat_of(r: int) -> Optional[str]:
                if _filled(t.rows[r]) < 2 or t.row_meta[r].kind == "summary":
                    return None
                key = row_key(t.rows[r])
                if key in seen:
                    rid, page = seen[key]
                    return f"Exactly the same as row {rid} (page {page})"
                seen[key] = (t.row_meta[r].row_id, t.row_meta[r].page)
                return None

            _remove(t, out, repeat_of)
    return out


_GENERATED = re.compile(r"(Column|Col)[ _]?\d+", re.I)


def _remove(t: LogicalTable, out: Cleaned, reason_for) -> None:
    """Drop the rows for which reason_for(row) returns a reason, recording each on the Removed rows list."""
    rows, metas = [], []
    for r in range(len(t.rows)):
        reason = reason_for(r)
        if reason is None:
            rows.append(t.rows[r])
            metas.append(t.row_meta[r])
        else:
            m = t.row_meta[r]
            out.removed.append(RemovedRow(t.table_id, m.row_id, m.page, list(t.rows[r]), reason))
    t.rows, t.row_meta = rows, metas
