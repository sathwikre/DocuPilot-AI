"""Key/value fields outside tables (subtotal, tax, grand total, opening/closing balance...) and
document-type classification. Purely rule-based."""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from core.models import DocField, LogicalTable, Word
from core.table_detector import split_segments, words_text
from core.value_parser import parse_number

# Order matters: more specific labels first.
FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [(k, re.compile(p, re.I)) for k, p in [
    ("total_debits", r"total\s+(debits?|withdrawals?|payments?\s+out)"),
    ("total_credits", r"total\s+(credits?|deposits?|payments?\s+in)"),
    ("opening_balance", r"(opening|beginning|starting|previous)\s+balance|balance\s+(b/?f|brought\s+forward)"),
    ("closing_balance", r"(closing|ending|new)\s+balance|balance\s+(c/?f|carried\s+forward)"),
    ("subtotal", r"sub[\s-]?total|net\s+total|total\s+before\s+tax|taxable\s+(value|amount)"),
    ("tax_total", r"(total\s+)?(tax|vat|gst|igst|cgst|sgst|sales\s+tax)(\s+total)?(\s+amount)?(\s*\(?\s*[\d.]+\s*%\s*\)?)?"),
    ("shipping", r"shipping|freight|delivery(\s+charges?)?|handling"),
    ("discount_total", r"(total\s+)?discount"),
    ("grand_total", r"grand\s+total|total\s+(due|payable|amount(\s+due)?)|amount\s+(due|payable)|invoice\s+total|balance\s+due|total"),
]]

_VALUE = r"[\(\-]?\s*(?:[$€£₹]|USD|EUR|GBP|INR)?\s*\d[\d,]*(?:\.\d{1,2})?\s*\)?(?:\s*(?:CR|DR))?"
_INLINE = re.compile(rf"^(?P<label>[A-Za-z][A-Za-z /&().%\-]*?)\s*[:=\-]?\s*(?P<value>{_VALUE})$")


def classify_label(label: str) -> str | None:
    lab = re.sub(r"\s+", " ", label.strip().rstrip(":").strip()).lower()
    for key, pat in FIELD_PATTERNS:
        if pat.fullmatch(lab):
            return key
    return None


def fields_from_lines(lines: Iterable[Sequence[Word]], page: int) -> list[DocField]:
    """Find 'Label: value' and 'Label ........ value' pairs in free-text lines."""
    out: list[DocField] = []
    for ln in lines:
        segs = [words_text(s) for s in split_segments(list(ln))]
        consumed = set()
        for i, seg in enumerate(segs):
            m = _INLINE.match(seg)
            if m:
                key = classify_label(m.group("label"))
                p = parse_number(m.group("value"))
                if key and p is not None:
                    out.append(DocField(key, m.group("label").strip(" :"), m.group("value"), p.value, page, "text"))
                    consumed.add(i)
        # Label segment followed by a number-only segment (right-aligned totals blocks).
        for i in range(len(segs) - 1):
            if i in consumed or i + 1 in consumed:
                continue
            key = classify_label(segs[i])
            p = parse_number(segs[i + 1])
            if key and p is not None:
                out.append(DocField(key, segs[i].strip(" :"), segs[i + 1], p.value, page, "text"))
    return out


def fields_from_summary_rows(table: LogicalTable) -> list[DocField]:
    """Turn classified summary rows (e.g. a 'Subtotal' row inside the table) into fields."""
    out = []
    for ri, m in enumerate(table.row_meta):
        if m.kind != "summary":
            continue
        row = table.rows[ri]
        label = next((c for c in row if c and parse_number(c) is None), "")
        nums = [(ci, parse_number(c)) for ci, c in enumerate(row) if c and parse_number(c) is not None]
        key = classify_label(label)
        if key and nums:
            ci, p = nums[-1]
            out.append(DocField(key, label, row[ci], p.value, m.page, "table-row", table.table_id))
    return out


def dedupe_fields(fields: list[DocField]) -> list[DocField]:
    seen, out = set(), []
    for f in fields:
        k = (f.key, str(f.value), f.page)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def detect_doc_type(text: str, tables: Sequence[LogicalTable], title: str = "") -> str:
    """Rule-based document type: invoice | bank_statement | purchase_order | inventory | financial_report | generic.

    `title` is the first text line of the document; a type keyword there carries extra weight.
    """
    t = text.lower()
    head = title.lower()
    roles = {r for tb in tables for r in tb.roles if r}
    scores = {
        "invoice": 2 * ("invoice" in t) + ("bill to" in t) + ("subtotal" in t or "sub total" in t)
        + 2 * ({"quantity", "unit_price"} <= roles) + ("tax_amount" in roles or "tax_rate" in roles),
        "bank_statement": 2 * ("statement" in t) + ("opening balance" in t) + ("closing balance" in t)
        + ("account number" in t) + 2 * ("balance" in roles and bool({"debit", "credit", "amount"} & roles)),
        "purchase_order": 3 * ("purchase order" in t) + ("po number" in t and "invoice" not in t),
        "inventory": 2 * ("inventory" in t) + ("warehouse" in t) + ("on hand" in t) + ("stock" in t),
        "financial_report": 2 * ("report" in t) + ("fy" in t) + ("quarter" in t or "q1" in t) + ("revenue" in t or "sales" in t),
    }
    title_words = {"invoice": ("invoice",), "bank_statement": ("statement",), "purchase_order": ("purchase order",),
                   "inventory": ("inventory", "stock"), "financial_report": ("report", "financial", "results")}
    for k, kws in title_words.items():
        if any(kw in head for kw in kws):
            scores[k] += 3
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else "generic"
