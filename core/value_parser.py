"""Deterministic parsing of numbers, percentages and dates found in table cells."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

_CURRENCY = r"(?:[$€£₹¥]|USD|EUR|GBP|INR|AUD|CAD|Rs\.?)"
_NUM_RE = re.compile(
    rf"""^\s*
    (?P<sign1>[-+−–])?\s*
    (?P<open>\()?\s*
    (?P<cur1>{_CURRENCY})?\s*
    (?P<sign2>[-−–])?\s*
    (?P<num>\d[\d,.' ]*\d|\d)\s*
    (?P<pct>%)?\s*
    (?P<cur2>{_CURRENCY})?\s*
    (?P<close>\))?\s*
    (?P<drcr>CR|DR|Cr|Dr)?\s*
    (?P<sign3>-)?\s*$""",
    re.VERBOSE,
)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MONTHS.update({"sept": 9, "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
                "august": 8, "september": 9, "october": 10, "november": 11, "december": 12})

_NUMERIC_DATE = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")
_TEXT_DATE_DMY = re.compile(r"^\s*(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})\.?[\s\-/.,]+(\d{2,4})\s*$")
_TEXT_DATE_MDY = re.compile(r"^\s*([A-Za-z]{3,9})\.?[\s\-/.]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-/.]+(\d{2,4})\s*$")

# OCR confusions that are safe to repair *only* inside cells of a numeric column.
_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})
_SWAP_SEPARATORS = str.maketrans({",": ".", ".": ","})
# Invisible / non-standard spaces that survive PDF text extraction.
_HIDDEN_CHARS = re.compile(r"[  -​  　﻿­]")


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal
    is_percent: bool
    negative_style: Optional[str]  # minus | parens | trailing | None
    currency: Optional[str]
    drcr: Optional[str]
    decimals: int


def _normalise_digits(num: str) -> Optional[tuple[str, int]]:
    """Resolve thousands/decimal separators. Returns (plain_number_string, n_decimals)."""
    s = num.replace(" ", "").replace("'", "")
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return None
    has_c, has_d = "," in s, "." in s
    if has_c and has_d:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        int_part, _, frac = s.rpartition(dec)
        if thou in frac or dec in int_part:
            return None
        if not re.fullmatch(r"\d{1,3}(" + re.escape(thou) + r"\d{2,3})*", int_part):
            return None
        int_part = int_part.replace(thou, "")
        if not frac.isdigit():
            return None
        return f"{int_part}.{frac}", len(frac)
    if has_c:
        # 1,234 / 1,23,456 (Indian grouping) -> thousands; 12,5 or 12,50 -> decimal comma
        if re.fullmatch(r"\d{1,3}(,\d{2,3})*,\d{3}", s):
            return s.replace(",", ""), 0
        if re.fullmatch(r"\d+,\d{1,2}", s):
            return s.replace(",", "."), len(s.split(",")[1])
        return None
    if has_d:
        if re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", s):
            return s.replace(".", ""), 0
        if s.count(".") == 1:
            return s, len(s.split(".")[1])
        return None
    return s, 0


def parse_number(text: Optional[str], decimal_comma: bool = False) -> Optional[ParsedNumber]:
    """Parse a cell as a number. Handles currency, thousands separators, (negatives), %, CR/DR.

    decimal_comma=True reads European numbers: '1.234,56' -> 1234.56 and '1.234' -> 1234.
    """
    if text is None:
        return None
    t = str(text).strip()
    if decimal_comma:
        t = t.translate(_SWAP_SEPARATORS)
    if not t or len(t) > 40:
        return None
    m = _NUM_RE.match(t)
    if not m:
        return None
    if bool(m.group("open")) != bool(m.group("close")):
        return None
    norm = _normalise_digits(m.group("num"))
    if norm is None:
        return None
    plain, decimals = norm
    try:
        val = Decimal(plain)
    except InvalidOperation:
        return None
    style = None
    if m.group("open"):
        style = "parens"
    elif m.group("sign1") in ("-", "−", "–") or m.group("sign2"):
        style = "minus"
    elif m.group("sign3"):
        style = "trailing"
    if style:
        val = -val
    cur = m.group("cur1") or m.group("cur2")
    drcr = (m.group("drcr") or "").upper() or None
    return ParsedNumber(val, bool(m.group("pct")), style, cur, drcr, decimals)


def clean_spaces(text: Optional[str]) -> str:
    """normalise_text plus removal of non-breaking, zero-width and other hidden characters."""
    if text is None:
        return ""
    return normalise_text(_HIDDEN_CHARS.sub(lambda m: "" if m.group(0) in "​﻿­" else " ", str(text)))


def ocr_repair_number(text: str) -> Optional[tuple[ParsedNumber, str]]:
    """Try to parse a numeric cell after repairing common OCR letter/digit confusions.

    Only characters adjacent to digits are translated, and only when the original already
    contains digits. Returns (parsed, repaired_text) or None.
    """
    if not text or not re.search(r"\d", text):
        return None
    # Low-resolution scans often turn thousands commas into periods: '10.695.60' -> '10,695.60'.
    # Unambiguous only when every group before the last separator has exactly 3 digits.
    m = re.fullmatch(r"([\(\-]?[$€£₹]?\s*)(\d{1,3}(?:\.\d{3})+)\.(\d{2})(\)?)", text.strip())
    if m:
        repaired = f"{m.group(1)}{m.group(2).replace('.', ',')}.{m.group(3)}{m.group(4)}"
        parsed = parse_number(repaired)
        if parsed:
            return parsed, repaired
    repaired = re.sub(
        r"(?<=[\d,.])[OoDlI|SB]|[OoDlI|SB](?=[\d,.])",
        lambda m: m.group(0).translate(_OCR_DIGIT_FIX), text)
    repaired = re.sub(r"(?<=\d)\s+(?=[.,]\d)|(?<=[.,])\s+(?=\d)", "", repaired)
    if repaired == text:
        return None
    parsed = parse_number(repaired)
    return (parsed, repaired) if parsed else None


def _year(y: int) -> int:
    if y < 100:
        return 2000 + y if y < 70 else 1900 + y
    return y


def _month(name: str) -> Optional[int]:
    n = name.lower().rstrip(".")
    return _MONTHS.get(n) or _MONTHS.get(n[:3])


def parse_date(text: Optional[str], order: Optional[str] = None) -> Optional[date]:
    """Parse common date formats. order is 'DMY' or 'MDY' for ambiguous numeric dates."""
    if text is None:
        return None
    t = str(text).strip()
    if not t or len(t) > 30:
        return None
    try:
        m = _NUMERIC_DATE.match(t)
        if m:
            a, b, c = (int(x) for x in m.groups())
            if len(m.group(1)) == 4:  # ISO-like Y-M-D
                return date(a, b, c)
            if len(m.group(3)) == 3:
                return None
            y = _year(c)
            if order == "MDY":
                return date(y, a, b)
            if order == "DMY":
                return date(y, b, a)
            if a > 12:
                return date(y, b, a)
            if b > 12:
                return date(y, a, b)
            return date(y, b, a)  # default DMY; callers infer order per column first
        m = _TEXT_DATE_DMY.match(t)
        if m and _month(m.group(2)):
            return date(_year(int(m.group(3))), _month(m.group(2)), int(m.group(1)))
        m = _TEXT_DATE_MDY.match(t)
        if m and _month(m.group(1)):
            return date(_year(int(m.group(3))), _month(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return None


def infer_date_order(values: Iterable[Optional[str]]) -> Optional[str]:
    """Infer DMY vs MDY for a column of numeric dates. None if undecidable."""
    dmy = mdy = False
    for v in values:
        m = _NUMERIC_DATE.match(str(v or ""))
        if not m or len(m.group(1)) == 4:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            dmy = True
        if b > 12:
            mdy = True
    if dmy and not mdy:
        return "DMY"
    if mdy and not dmy:
        return "MDY"
    return None


def looks_numeric(text: Optional[str]) -> bool:
    return parse_number(text) is not None


def looks_date(text: Optional[str]) -> bool:
    return parse_date(text) is not None


def cell_kind(text: Optional[str]) -> str:
    """Coarse lexical kind of a cell: empty | number | date | text."""
    if text is None or not str(text).strip():
        return "empty"
    if looks_date(text):
        return "date"
    if looks_numeric(text):
        return "number"
    return "text"


def normalise_text(text: Optional[str]) -> str:
    """Collapse whitespace (including line breaks inside wrapped cells)."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()
