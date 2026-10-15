"""Step C: header normalisation and semantic column roles.

Deterministic rules first: abbreviation expansion + a synonym dictionary per semantic role, with a
tight fuzzy match for OCR typos. Distinct concepts (Debit vs Credit, Posting vs Value date, Tax rate
vs Tax amount) are separate roles and are never collapsed into one another.
Optional local-LLM assistance is applied only to headers the rules could not map.
"""
from __future__ import annotations

import difflib
import re
from typing import Callable, Optional, Sequence

from core.value_parser import normalise_text

# Abbreviations expanded token-by-token before matching.
_ABBREV = {
    "txn": "transaction", "trx": "transaction", "trans": "transaction", "tx": "transaction",
    "qty": "quantity", "amt": "amount", "desc": "description", "descr": "description",
    "ref": "reference", "no": "number", "num": "number", "nr": "number", "#": "number",
    "dr": "debit", "cr": "credit", "bal": "balance", "acct": "account", "acc": "account",
    "inv": "invoice", "pct": "percent", "%": "percent", "disc": "discount", "uom": "unit",
    "chq": "cheque", "chk": "check", "sl": "serial", "sr": "serial", "val": "value",
    "dt": "date", "ext": "extended", "incl": "including", "excl": "excluding", "w/d": "withdrawal",
}

# role -> (display name, synonyms after abbreviation expansion)
ROLES: dict[str, tuple[str, set[str]]] = {
    "line_no": ("Line No", {"number", "line", "line number", "serial", "serial number", "s number", "item number",
                            "line item", "sl", "s no", "sno"}),
    "date": ("Date", {"date"}),
    "transaction_date": ("Transaction Date", {"transaction date", "date of transaction", "transaction dt"}),
    "posting_date": ("Posting Date", {"posting date", "post date", "booking date", "book date"}),
    "value_date": ("Value Date", {"value date", "effective date"}),
    "invoice_date": ("Invoice Date", {"invoice date", "bill date"}),
    "due_date": ("Due Date", {"due date", "payment due", "due"}),
    "description": ("Description", {"description", "particulars", "details", "narration", "item description",
                                    "transaction details", "memo", "description of goods", "transaction description"}),
    "reference": ("Reference", {"reference", "reference number", "cheque number", "check number",
                                "transaction id", "transaction reference", "ref number", "utr"}),
    "debit": ("Debit", {"debit", "debits", "withdrawal", "withdrawals", "debit amount", "money out", "paid out",
                        "debit transaction"}),
    "credit": ("Credit", {"credit", "credits", "deposit", "deposits", "credit amount", "money in", "paid in"}),
    "balance": ("Balance", {"balance", "running balance", "balance amount", "available balance", "closing balance"}),
    "amount": ("Amount", {"amount", "transaction amount", "value"}),
    "quantity": ("Quantity", {"quantity", "units", "quantity ordered", "ordered quantity", "quantity shipped", "pcs"}),
    "unit": ("Unit", {"unit", "unit of measure", "units of measure"}),
    "unit_price": ("Unit Price", {"unit price", "price", "rate", "unit cost", "price per unit", "unit rate",
                                  "price each", "cost per unit"}),
    "discount": ("Discount", {"discount", "discount percent", "discount amount", "discount rate"}),
    "net_amount": ("Net Amount", {"net amount", "net", "taxable value", "taxable amount", "amount before tax",
                                  "net value", "subtotal"}),
    "tax_rate": ("Tax Rate", {"tax percent", "tax rate", "vat percent", "vat rate", "gst percent", "gst rate",
                              "tax percentage"}),
    "tax_amount": ("Tax Amount", {"tax amount", "vat amount", "gst amount", "tax", "vat", "gst", "tax value"}),
    "line_total": ("Line Total", {"line total", "total", "total amount", "gross amount", "amount including tax",
                                  "extended price", "extended amount", "line amount", "total price", "row total",
                                  "fy total", "grand total"}),
    "sku": ("SKU", {"sku", "item code", "product code", "part number", "article number", "code"}),
    "item": ("Item", {"item", "product", "item name", "product name", "article"}),
    "hsn": ("HSN Code", {"hsn", "hsn code", "hsn sac", "sac"}),
    "invoice_no": ("Invoice No", {"invoice number", "invoice", "bill number"}),
    "paid": ("Paid", {"paid", "amount paid", "paid amount", "payment"}),
    "outstanding": ("Outstanding", {"outstanding", "balance due", "amount due", "outstanding amount", "open amount"}),
    "status": ("Status", {"status", "state"}),
    "currency": ("Currency", {"currency", "ccy", "cur"}),
}

_LOOKUP: dict[str, str] = {syn: role for role, (_d, syns) in ROLES.items() for syn in syns}
_FUZZY_MIN = 0.86
# Roles whose original header text is usually more informative than a canonical name.
_KEEP_ORIGINAL = {"line_total", "discount", "tax_rate", "line_no"}


def header_tokens(text: str) -> str:
    """Lower-case, punctuation-stripped, abbreviation-expanded form of a header."""
    t = normalise_text(text).lower()
    t = t.replace("%", " % ").replace("#", " # ").replace("/", " / ")
    toks = re.findall(r"[a-z0-9]+|%|#", t)
    out = [_ABBREV.get(tok, tok) for tok in toks]
    return " ".join(out).strip()


def match_role(text: str) -> tuple[Optional[str], str]:
    """Return (role, how) for a header string. how in {'exact','fuzzy','none'}."""
    key = header_tokens(text)
    if not key:
        return None, "none"
    if key in _LOOKUP:
        return _LOOKUP[key], "exact"
    # Allow a trailing 'percent' variant (e.g. "Disc %") to map onto its base concept.
    if key.endswith(" percent") and key[: -len(" percent")] in _LOOKUP:
        base = _LOOKUP[key[: -len(" percent")]]
        if base == "tax_amount":
            return "tax_rate", "exact"
        return base, "exact"
    best, best_ratio = None, 0.0
    for syn, role in _LOOKUP.items():
        if abs(len(syn) - len(key)) > 3 or syn[0] != key[0]:
            continue
        r = difflib.SequenceMatcher(None, key, syn).ratio()
        if r > best_ratio:
            best, best_ratio = role, r
    if best and best_ratio >= _FUZZY_MIN and len(key) >= 5:
        return best, "fuzzy"
    return None, "none"


def clean_header(text: str) -> str:
    t = normalise_text(text)
    return t if t else ""


def refine_roles_by_values(roles: list[Optional[str]], col_types: Sequence[str]) -> list[str]:
    """Adjust roles using column value types. Returns operation notes."""
    notes = []
    for i, (role, ctype) in enumerate(zip(roles, col_types)):
        if role == "tax_amount" and ctype == "percent":
            roles[i] = "tax_rate"
            notes.append(f"column {i + 1}: values are percentages, role tax_amount -> tax_rate")
        elif role == "tax_rate" and ctype in ("number", "integer"):
            pass
        elif role == "discount" and ctype == "percent":
            roles[i] = "discount_rate"
            notes.append(f"column {i + 1}: discount expressed as percentage")
        elif role == "discount" and ctype in ("number", "integer"):
            roles[i] = "discount_amount"
        elif role in ("debit", "credit", "balance", "amount", "unit_price", "line_total", "net_amount",
                      "tax_amount", "quantity") and ctype in ("text", "empty"):
            notes.append(f"column {i + 1}: role {role} dropped because values are not numeric")
            roles[i] = None
    return notes


def normalise_headers(
    header: Sequence[str],
    llm_mapper: Optional[Callable[[list[str], list[str]], dict[str, str]]] = None,
) -> tuple[list[str], list[Optional[str]], list[str]]:
    """Map original headers to display names + roles.

    Returns (display_names, roles, ops). Display names are unique; originals are kept by the caller.
    """
    display: list[str] = []
    roles: list[Optional[str]] = []
    ops: list[str] = []
    used_roles: set[str] = set()
    for i, h in enumerate(header):
        role, how = match_role(h)
        original = clean_header(h)
        if role and role in used_roles and role not in ("amount",):
            # Two columns mapped to the same concept: keep the second one unmapped and say so.
            ops.append(f"'{original}' also matched role '{role}'; kept original header to avoid a collision")
            role = None
        if role:
            used_roles.add(role)
            canonical = ROLES[role][0]
            plain = " ".join(re.findall(r"[a-z0-9]+|%|#", original.lower()))
            expanded = header_tokens(original)
            # Canonicalise only superficial variants: abbreviations ('Txn Date'), OCR typos (fuzzy) and
            # exact matches. Informative originals ('Unit Cost', 'FY Total', 'Disc %') are kept as written.
            if role in _KEEP_ORIGINAL and original:
                name = original
            elif how == "fuzzy" or expanded != plain or expanded == header_tokens(canonical):
                name = canonical
            else:
                name = original
            if name != original:
                ops.append(f"header '{original}' -> '{name}' ({how} match, role {role})")
            display.append(name)
        else:
            display.append(original or f"Column {i + 1}")
            if not original:
                ops.append(f"column {i + 1} had no header text; named 'Column {i + 1}'")
        roles.append(role)

    if llm_mapper is not None:
        unmapped = [display[i] for i, r in enumerate(roles) if r is None and display[i]]
        if unmapped:
            allowed = [r for r in ROLES if r not in used_roles]
            try:
                suggestions = llm_mapper(unmapped, allowed)
            except Exception as exc:  # LLM is optional: never fail the pipeline because of it
                suggestions = {}
                ops.append(f"local LLM header mapping skipped ({type(exc).__name__})")
            for i, r in enumerate(roles):
                if r is None and display[i] in suggestions:
                    sug = suggestions[display[i]]
                    if sug in ROLES and sug not in used_roles:
                        roles[i] = sug
                        used_roles.add(sug)
                        ops.append(f"local LLM suggested role '{sug}' for header '{display[i]}' (header text kept)")

    # Ensure unique display names.
    seen: dict[str, int] = {}
    for i, n in enumerate(display):
        k = n.lower()
        if k in seen:
            seen[k] += 1
            display[i] = f"{n} ({seen[k]})"
            ops.append(f"duplicate header '{n}' renamed to '{display[i]}'")
        else:
            seen[k] = 1
    return display, roles, ops


def header_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """0..1 similarity between two header rows (order-sensitive), tolerant of OCR noise."""
    if not a or not b:
        return 0.0
    n = max(len(a), len(b))
    total = 0.0
    for x, y in zip(a, b):
        kx, ky = header_tokens(x), header_tokens(y)
        if not kx and not ky:
            total += 1.0
            continue
        rx, _ = match_role(x)
        ry, _ = match_role(y)
        if rx and rx == ry:
            total += 1.0
            continue
        total += difflib.SequenceMatcher(None, kx, ky).ratio()
    return total / n
