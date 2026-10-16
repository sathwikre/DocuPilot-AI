"""Reusable UI building blocks: icon set, cards, trackers, issue cards. Returns HTML strings.

HTML is emitted without leading indentation so Streamlit's markdown renderer never turns it into a
code block. All user/document-derived text is escaped.
"""
from __future__ import annotations

import html as _html
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import streamlit as st

# Lucide-style 24px stroke icons (ISC-licensed shapes, redrawn inline; no network requests).
_P = 'fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"'
ICONS = {
    "logo": f'<svg viewBox="0 0 24 24" {_P}><path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/><path d="M9 13h6M9 17h4"/><path d="M8.5 9.5 10 8l1.5 1.5"/></svg>',
    "upload": f'<svg viewBox="0 0 24 24" {_P}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg>',
    "scan": f'<svg viewBox="0 0 24 24" {_P}><path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/></svg>',
    "shield": f'<svg viewBox="0 0 24 24" {_P}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
    "download": f'<svg viewBox="0 0 24 24" {_P}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>',
    "file": f'<svg viewBox="0 0 24 24" {_P}><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><path d="M14 2v6h6"/></svg>',
    "table": f'<svg viewBox="0 0 24 24" {_P}><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/></svg>',
    "rows": f'<svg viewBox="0 0 24 24" {_P}><path d="M3 6h18M3 12h18M3 18h18"/></svg>',
    "columns": f'<svg viewBox="0 0 24 24" {_P}><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18"/></svg>',
    "pages": f'<svg viewBox="0 0 24 24" {_P}><path d="M8 3h9a2 2 0 0 1 2 2v12"/><rect x="5" y="7" width="12" height="14" rx="2"/></svg>',
    "alert": f'<svg viewBox="0 0 24 24" {_P}><path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3z"/><path d="M12 9v4M12 17h.01"/></svg>',
    "octagon": f'<svg viewBox="0 0 24 24" {_P}><path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z"/><path d="M12 8v4M12 16h.01"/></svg>',
    "info": f'<svg viewBox="0 0 24 24" {_P}><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
    "check": f'<svg viewBox="0 0 24 24" {_P}><path d="M20 6 9 17l-5-5"/></svg>',
    "check_circle": f'<svg viewBox="0 0 24 24" {_P}><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>',
    "clock": f'<svg viewBox="0 0 24 24" {_P}><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    "merge": f'<svg viewBox="0 0 24 24" {_P}><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/></svg>',
    "sparkles": f'<svg viewBox="0 0 24 24" {_P}><path d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M5 3v4M3 5h4M19 17v4M17 19h4"/></svg>',
    "cpu": f'<svg viewBox="0 0 24 24" {_P}><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></svg>',
    "lock": f'<svg viewBox="0 0 24 24" {_P}><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
    "history": f'<svg viewBox="0 0 24 24" {_P}><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/></svg>',
    "eye": f'<svg viewBox="0 0 24 24" {_P}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    "arrow": f'<svg viewBox="0 0 24 24" {_P}><path d="M5 12h14M12 5l7 7-7 7"/></svg>',
    "wand": f'<svg viewBox="0 0 24 24" {_P}><path d="m15 4 5 5L9 20H4v-5z"/><path d="m13 6 5 5"/></svg>',
    "chart": f'<svg viewBox="0 0 24 24" {_P}><path d="M3 3v18h18"/><path d="M7 16v-5M12 16V8M17 16v-8"/></svg>',
    "x": f'<svg viewBox="0 0 24 24" {_P}><path d="M18 6 6 18M6 6l12 12"/></svg>',
}

SEVERITY_ICON = {"critical": "octagon", "warning": "alert", "info": "info", "passed": "check_circle"}
SEVERITY_LABEL = {"critical": "Critical", "warning": "Warning", "info": "Informational", "passed": "Passed"}
CHECK_LABELS = {
    "line_arithmetic": "Line arithmetic", "line_total_sum": "Line totals vs. totals block", "subtotal": "Subtotal",
    "tax_total": "Tax total", "grand_total": "Grand total", "running_balance": "Running balance",
    "statement_reconciliation": "Opening + movement = closing", "closing_balance": "Closing balance",
    "total_debits": "Total debits", "total_credits": "Total credits", "row_total": "Row total",
    "column_total": "Column total", "data_type": "Data type consistency", "missing_value": "Missing value",
    "column_shift": "Column alignment", "duplicate_rows": "Duplicate-looking rows", "sequence_gap": "Row sequence",
    "repeated_header": "Repeated header", "table_merge": "Multi-page merge", "row_width": "Row width",
    "boundary_duplicate": "Page-boundary duplicate", "header_detection": "Header detection",
    "page_processing": "Page processing", "ocr_confidence": "OCR confidence", "tables_found": "Table detection",
    "date_order": "Date order",
}
DOC_TYPES = {"invoice": "Invoice", "bank_statement": "Bank statement", "purchase_order": "Purchase order",
             "inventory": "Inventory record", "financial_report": "Financial report", "generic": "Tabular document"}


def esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def icon(name: str) -> str:
    return ICONS.get(name, "")


def render(html: str) -> None:
    st.markdown(html, unsafe_allow_html=True)


def inject_css() -> None:
    css = (Path(__file__).parent / "styles.css").read_text(encoding="utf-8")
    render(f"<style>{css}</style>")


_LOGO = Path(__file__).parent / "assets" / "incuxai_logo.png"


@st.cache_data(show_spinner=False)
def _logo_data_uri() -> str:
    import base64
    try:
        return "data:image/png;base64," + base64.b64encode(_LOGO.read_bytes()).decode()
    except OSError:
        return ""


def brand() -> str:
    """The sidebar header: the IncuXai logo; the text brand only if the logo file is missing."""
    uri = _logo_data_uri()
    if uri:
        return f'<div class="dp-org-logo"><img src="{uri}" alt="IncuXai — AI for smart thinking"></div>'
    return ('<div class="dp-brand"><div class="dp-brand-mark">' + icon("logo") + '</div>'
            '<div><div class="dp-brand-name">DocuPilot AI</div><div class="dp-brand-sub">PDF · SCAN · PHOTO → EXCEL</div></div></div>')


def section(title: str, ic: str = "sparkles") -> str:
    return f'<div class="dp-section">{icon(ic)}<h3>{esc(title)}</h3><div class="line"></div></div>'


def page_title(title: str, subtitle: str = "", right_html: str = "") -> str:
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    return f'<div class="dp-page-title"><div><h2>{esc(title)}</h2>{sub}</div><div class="dp-chips">{right_html}</div></div>'


def chip(text: str, kind: str = "", ic: Optional[str] = None) -> str:
    return f'<span class="dp-chip {kind}">{icon(ic) if ic else ""}{esc(text)}</span>'


def kpi(label: str, value: Any, ic: str, foot: str = "", kind: str = "", unit: str = "") -> str:
    u = f"<small>{esc(unit)}</small>" if unit else ""
    f = f'<div class="foot">{esc(foot)}</div>' if foot else ""
    return (f'<div class="dp-kpi {kind}"><div class="top"><span>{esc(label)}</span>{icon(ic)}</div>'
            f'<div class="val">{esc(value)}{u}</div>{f}</div>')


def kpi_grid(cards: Iterable[str], cols: int = 4) -> str:
    return f'<div class="dp-kpis{" two" if cols == 2 else ""}">' + "".join(cards) + "</div>"


def workflow() -> str:
    steps = [("upload", "Upload", "PDF, scan or image"), ("scan", "Extract", "Text, OCR & tables"),
             ("shield", "Validate", "Totals, balances, structure"), ("download", "Export", "Formatted .xlsx")]
    out = '<div class="dp-flow">'
    for i, (ic, t, s) in enumerate(steps, start=1):
        out += f'<div class="dp-flow-step"><div class="ic">{icon(ic)}</div><div><b>{t}</b><span>{s}</span></div><div class="n">0{i}</div></div>'
    return out + "</div>"


def empty_state(title: str, body: str, ic: str = "file") -> str:
    return f'<div class="dp-empty"><div class="ic">{icon(ic)}</div><h4>{esc(title)}</h4><p>{body}</p></div>'


def note(text: str, kind: str = "", ic: str = "info") -> str:
    return f'<div class="dp-note {kind}">{icon(ic)}<div>{text}</div></div>'


def stage_tracker(stages: Sequence[str], state: dict, file_name: str) -> str:
    """state: {'current': idx, 'details': {idx: str}, 'times': {idx: float}, 'units': (done, total), 'elapsed': s,
               'failed': bool}"""
    cur = state.get("current", 0)
    done_units, total_units = state.get("units", (0, 0))
    frac = (done_units / total_units) if total_units else 0.0
    finished = state.get("finished", set())
    progress = (len(finished) + (0 if cur in finished else frac)) / len(stages)
    if state.get("done") or state.get("failed"):
        meta = chip(f"{state.get('elapsed', 0):.1f}s elapsed", "ghost", "clock")
    else:
        # Ticks in the browser between server events, starting from the real elapsed time.
        meta = (f'<span class="dp-chip ghost">{icon("clock")}<span class="dp-tick" '
                f'style="animation-delay:-{state.get("elapsed", 0):.2f}s"></span></span>')
    if state.get("pages"):
        meta += chip(f"{state['pages']} page{'s' if state['pages'] != 1 else ''}", "ghost", "pages")
    if state.get("ocr_pages"):
        meta += chip(f"OCR on {state['ocr_pages']}", "cyan", "scan")
    out = (f'<div class="dp-proc-head"><h3>Processing <span style="color:var(--violet-2)">{esc(file_name)}</span></h3>'
           f'<div class="meta">{meta}</div></div>'
           f'<div class="dp-bar"><div style="width:{max(progress, 0.02) * 100:.1f}%"></div></div><div class="dp-stages">')
    for i, name in enumerate(stages):
        if i in finished:
            cls, dot = "done", icon("check")
        elif i == cur and not state.get("failed"):
            cls, dot = "active", str(i + 1)
        elif i == cur:
            cls, dot = "failed", icon("x")
        else:
            cls, dot = "", str(i + 1)
        detail = state.get("details", {}).get(i, "Waiting" if cls == "" else "")
        if cls == "active" and total_units:
            detail = f"{detail}"
        t = state.get("times", {}).get(i)
        tt = f"{t:.2f}s" if t is not None else ""
        out += (f'<div class="dp-stage {cls}"><div class="dot">{dot}</div><div><div class="name">{esc(name)}</div>'
                f'<div class="detail">{esc(detail)}</div></div><div class="time">{tt}</div></div>')
    return out + "</div>"


def fmt_val(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1e15 else str(v)
    return str(v)


def issue_card(i) -> str:
    sev = i.severity
    head = chip(SEVERITY_LABEL[sev], sev, SEVERITY_ICON[sev])
    if sev in ("critical", "warning"):
        head += chip("Confirmed" if i.confirmed else "Suspected", "violet" if i.confirmed else "ghost")
    head += chip(i.status, "ghost")
    loc = []
    for label, v in (("Table", i.table_id), ("Row", i.row_id), ("Page", i.page), ("Column", i.column)):
        if v not in (None, ""):
            loc.append(f"<span>{label} <b>{esc(v)}</b></span>")
    vals = ""
    if any(x is not None for x in (i.extracted, i.expected, i.difference)):
        neg = ' class="neg"' if isinstance(i.difference, (int, float)) and abs(i.difference) > 0 else ""
        vals = (f'<div class="vals"><div><span>Extracted</span><code>{esc(fmt_val(i.extracted))}</code></div>'
                f'<div><span>Expected</span><code>{esc(fmt_val(i.expected))}</code></div>'
                f'<div{neg}><span>Difference</span><code>{esc(fmt_val(i.difference))}</code></div></div>')
    act = f'<div class="act">{icon("arrow")}<span>{esc(i.suggested_action)}</span></div>' if i.suggested_action else ""
    title = CHECK_LABELS.get(i.check, i.check.replace("_", " ").title())
    return (f'<div class="dp-issue {sev}"><div class="hd"><span class="title">{esc(title)}</span>{head}</div>'
            f'<div class="loc">{"".join(loc)}</div>{vals}<div class="ex">{esc(i.explanation)}</div>{act}</div>')


def state_card(sev: str, count: int, sub: str) -> str:
    return (f'<div class="dp-state {sev}"><div class="lbl">{icon(SEVERITY_ICON[sev])}{SEVERITY_LABEL[sev]}</div>'
            f'<div class="num">{count}</div><div class="sub">{esc(sub)}</div></div>')


def html_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], num_cols: Sequence[int] = (),
               widths: Optional[Sequence[str]] = None) -> str:
    style = ' style="table-layout:fixed"' if widths else ""
    cols = "<colgroup>" + "".join(f'<col style="width:{w}">' for w in widths) + "</colgroup>" if widths else ""
    out = (f'<div class="dp-table-scroll" role="region" aria-label="Scrollable data table" tabindex="0">'
           f'<table class="dp-table"{style}>{cols}<thead><tr>' + "".join(f"<th>{esc(h)}</th>" for h in headers)
           + "</tr></thead><tbody>")
    for r in rows:
        out += "<tr>" + "".join(
            f'<td class="num">{c}</td>' if i in num_cols else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>"
    return out + "</tbody></table></div>"


def stat(label: str, value: Any, tone: str = "") -> str:
    """A compact number tile. tone: '' | bad | warn | good."""
    return f'<div class="dp-stat {tone}"><div class="v">{esc(value)}</div><div class="l">{esc(label)}</div></div>'


def page_strip(reports) -> str:
    """One square per page, coloured by how it was read, with a legend."""
    cells = []
    for p in reports:
        if p.status == "failed":
            mode, how = "failed", "could not be read"
        elif p.ocr_used:
            mode = "ocr"
            how = f"read by OCR, {p.ocr_confidence:.0%} confidence" if p.ocr_confidence else "read by OCR"
        elif p.kind == "empty":
            mode, how = "blank", "blank page"
        else:
            mode, how = "digital", "text read directly"
        cells.append(f'<span class="sq {mode}" title="Page {p.page}: {esc(how)} · {p.tables_found} table piece(s)"></span>')
    legend = "".join(f'<span><i class="sq {m}"></i>{t}</span>' for m, t in
                     (("digital", "text read directly"), ("ocr", "read by OCR"), ("blank", "blank"), ("failed", "failed")))
    return (f'<div class="dp-strip" aria-label="How each page was read">{"".join(cells)}</div>'
            f'<div class="dp-legend">{legend}</div>')


def _display(v: Any, ctype: str) -> str:
    from datetime import date as _date
    from decimal import Decimal as _Dec
    if v is None:
        return ""
    if isinstance(v, _Dec):
        if ctype == "percent":
            return f"{(v * 100).normalize():f}%"
        return f"{v:,}"
    if isinstance(v, _date):
        return v.isoformat()
    return str(v)


def preview_table(t, issues, limit: int = 25) -> str:
    """The first `limit` rows of a table with flagged cells coloured (tooltip = the finding)."""
    flags: dict[tuple[int, int], tuple[str, str]] = {}
    for i in issues:
        if i.table_id != t.table_id or i.row_index is None or i.severity not in ("critical", "warning"):
            continue
        key = (i.row_index, i.col_index if i.col_index is not None else 0)
        if key not in flags or i.severity == "critical":
            flags[key] = (i.severity, i.explanation)
    numeric = {c for c, ty in enumerate(t.col_types) if ty in ("number", "integer", "percent")}
    head = "".join(f'<th class="{"num" if c in numeric else ""}">{esc(h)}</th>' for c, h in enumerate(t.columns))
    body = []
    for r in range(min(limit, len(t.values))):
        tds = []
        for c, v in enumerate(t.values[r]):
            sev, msg = flags.get((r, c), ("", ""))
            cls = " ".join(x for x in ("num" if c in numeric else "", sev) if x)
            tip = f' title="{esc(msg)}"' if msg else ""
            tds.append(f'<td class="{cls}"{tip}>{esc(_display(v, t.col_types[c] if c < len(t.col_types) else ""))}</td>')
        kind = ' class="total"' if t.row_meta[r].kind == "summary" else ""
        body.append(f"<tr{kind}>{''.join(tds)}</tr>")
    pages = f"page {t.pages[0]}" if len(t.pages) == 1 else f"pages {t.pages[0]}–{t.pages[-1]}"
    more = (f'<p class="dp-more">Showing the first {limit} of {len(t.values)} rows. The Excel file has all of them.</p>'
            if len(t.values) > limit else "")
    return (f'<h4 class="dp-tbl-title">{esc(t.title or t.table_id)} <span>· {len(t.values)} rows × {t.ncols} columns · '
            f'{pages}</span></h4><div class="dp-preview"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>{more}')


def check_grid(summary: Sequence[dict]) -> str:
    items = []
    for c in summary:
        ok = c["passed"] == c["checked"]
        items.append(f'<li><span>{esc(CHECK_LABELS.get(c["check"], c["check"]))}</span>'
                     f'<b class="{"good" if ok else "bad"}">{c["passed"]}/{c["checked"]}</b></li>')
    return f'<ul class="dp-checkgrid">{"".join(items)}</ul>'


def issue_line(i) -> str:
    """One-line finding for the results list."""
    where = " · ".join(str(x) for x in (i.table_id, f"page {i.page}" if i.page else "",
                                        f"row {i.row_id}" if i.row_id else "", i.column) if x)
    label = {"critical": "error", "warning": "warning", "info": "info", "passed": "passed"}[i.severity]
    return (f'<li class="dp-line {i.severity}"><b>{label}</b>{esc(i.explanation)}'
            f'{f"<span>{esc(where)}</span>" if where else ""}</li>')


def status_row(dot: str, title: str, sub: str) -> str:
    return f'<div class="dp-status-row"><div class="dp-dot {dot}"></div><div><b>{esc(title)}</b><span>{esc(sub)}</span></div></div>'
