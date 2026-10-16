"""DocuPilot AI — Streamlit application.

Run:  streamlit run app.py
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pymupdf as fitz
import streamlit as st

from config.settings import SETTINGS
from core import cleanup
from core.agent_orchestrator import STAGES, AgentOrchestrator, ProgressEvent, rebuild
from core.models import DocumentRejected, LogicalTable, ProcessingResult
from core.validator import check_summary
from ui import charts
from ui.components import (
    CHECK_LABELS, DOC_TYPES, SEVERITY_LABEL, brand, check_grid, chip, empty_state, esc, html_table, icon, inject_css,
    issue_card, issue_line, kpi, kpi_grid, note, page_strip, page_title, preview_table, render, section, stage_tracker,
    stat, state_card,
)
from utils.file_validation import FileValidationError, sanitize_filename, sha256_bytes
from utils.history import (
    clear_history, delete_run, load_history, load_result, record_run, run_files, storage_used, update_run,
)

ROOT = Path(__file__).resolve().parent
SAMPLES = {  # label -> (path, button icon, tooltip)
    "Bank statement": (ROOT / "samples" / "sample_b_statement_scanned.pdf", ":material/document_scanner:",
                          "Scanned bank statement · 3 pages · OCR"),
    "Complex report": (ROOT / "samples" / "sample_c_report.pdf", ":material/analytics:", "Financial report · 3 tables"),
}
NAV = ["Home", "Overview", "Table Explorer", "Validation Center", "Export Center", "History"]
NAV_ICONS = {"Home": ":material/home:", "Overview": ":material/space_dashboard:", "Table Explorer": ":material/table_view:",
             "Validation Center": ":material/verified:", "Export Center": ":material/download:", "History": ":material/history:"}

st.set_page_config(page_title="DocuPilot AI", page_icon="🧭", layout="wide", initial_sidebar_state="auto")
inject_css()

ss = st.session_state
ss.setdefault("results", {})          # file hash -> ProcessingResult (kept for this browser session only)
ss.setdefault("active", None)         # hash of the result being viewed
ss.setdefault("applied", None)        # seconds the last checklist rebuild took
if "_goto" in ss:
    ss["nav"] = ss.pop("_goto")
ss.setdefault("nav", "Home")


def active_result() -> Optional[ProcessingResult]:
    return ss.results.get(ss.active) if ss.active else None


def goto(page: str) -> None:
    ss["_goto"] = page
    st.rerun()


# =============================================================================== sidebar
with st.sidebar:
    render(brand())
    render('<div class="dp-side-label">Navigate</div>')
    st.radio("Navigation", NAV, key="nav", label_visibility="collapsed",
             format_func=lambda p: f"{NAV_ICONS[p]} {p}")
    res = active_result()
    if res:
        render('<div class="dp-side-label">Current document</div>')
        m = res.metrics()
        render(f'<div class="dp-doc-mini"><b>{esc(res.file_name)}</b>{esc(DOC_TYPES.get(res.doc_type, res.doc_type))} · '
               f'{m["pages"]} pages · {m["tables"]} tables · {m["rows"]} rows</div>')


# =============================================================================== helpers
def doc_info_preview(name: str, data: bytes) -> tuple[str, Optional[int]]:
    """Cheap pre-flight info for the selected file (type, page count) shown before processing."""
    kind = "PDF" if name.lower().endswith(".pdf") else "Image"
    pages = None
    try:
        if kind == "PDF":
            with fitz.open(stream=data, filetype="pdf") as d:
                pages = d.page_count if not d.needs_pass else None
        else:
            pages = 1
    except Exception:
        pages = None
    return kind, pages


def run_processing(name: str, data: bytes, ocr_mode: str, remove_repeats: bool, holder) -> Optional[ProcessingResult]:
    """Run the pipeline with a live, real-progress stage tracker."""
    state: dict[str, Any] = {"current": 0, "details": {}, "times": {}, "finished": set(), "units": (0, 0), "elapsed": 0.0}
    started = time.time()

    def on_progress(e: ProgressEvent) -> None:
        state["current"] = e.stage_index
        state["details"][e.stage_index] = e.detail
        state["units"] = (e.done_units, e.total_units)
        state["elapsed"] = e.elapsed
        if e.stage_index == 0 and e.finished:
            try:
                state["pages"] = int(e.detail.split()[0])
            except ValueError:
                pass
        if e.stage_index == 2 and "(OCR)" in e.detail:
            state["ocr_pages"] = state.get("ocr_pages", 0) + 1
        if e.finished and e.stage_index == len(STAGES) - 1:
            state["done"] = True
        if e.finished:
            state["finished"].add(e.stage_index)
            state["times"][e.stage_index] = e.elapsed - sum(state["times"].values())
        holder.markdown(stage_tracker(STAGES, state, name), unsafe_allow_html=True)

    holder.markdown(stage_tracker(STAGES, state, name), unsafe_allow_html=True)
    try:
        result = AgentOrchestrator(progress=on_progress, ocr_mode=ocr_mode, remove_repeats=remove_repeats).run(data, name)
    except (DocumentRejected, FileValidationError) as exc:
        state["failed"] = True
        state["details"][state["current"]] = str(exc)
        state["elapsed"] = time.time() - started
        holder.markdown(stage_tracker(STAGES, state, name), unsafe_allow_html=True)
        render(note(f"<b>Document rejected.</b> {esc(exc)}", "err", "octagon"))
        return None
    except Exception as exc:  # unexpected: show a clean message, keep details in the log
        state["failed"] = True
        holder.markdown(stage_tracker(STAGES, state, name), unsafe_allow_html=True)
        render(note(f"<b>Processing failed unexpectedly</b> ({esc(type(exc).__name__)}). Details were written to the "
                    "local log file.", "err", "octagon"))
        import logging
        logging.getLogger("docupilot.app").exception("processing failed")
        return None
    return result


def to_display(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


def table_frame(t: LogicalTable, issue_cells: dict) -> pd.DataFrame:
    """DataFrame of typed values plus provenance columns; mixed-type columns fall back to text."""
    meta_cols = {"Row ID", "Page", "Flags"}
    cols = [c if c not in meta_cols else f"{c} (data)" for c in t.columns]
    records = []
    for ri, (vals, m) in enumerate(zip(t.values, t.row_meta)):
        rec = {"Row ID": m.row_id, "Page": m.page}
        for c, v in zip(cols, vals):
            rec[c] = to_display(v)
        rec["Flags"] = sum(len(v) for (r, _c), v in issue_cells.items() if r == ri)
        records.append(rec)
    df = pd.DataFrame(records, columns=["Row ID", "Page", *cols, "Flags"])
    for c in cols:
        kinds = {type(v) for v in df[c] if v is not None and not (isinstance(v, float) and pd.isna(v))}
        if len(kinds) > 1:
            df[c] = [None if v is None else str(v) for v in df[c]]
    return df


def issue_cells_for(res: ProcessingResult, t: LogicalTable) -> dict[tuple[int, Optional[int]], list]:
    out: dict = {}
    for i in res.issues:
        if i.table_id == t.table_id and i.row_index is not None and i.severity in ("critical", "warning"):
            out.setdefault((i.row_index, i.col_index), []).append(i)
    return out


def no_result_state(what: str) -> None:
    render(empty_state("No document processed yet",
                       f"Upload a PDF, scanned document or image on the Home page to see {what} here. "
                       "Everything you see will come from your actual document.", "file"))
    st.write("")
    c = st.columns([1, 1, 1])[1]
    if c.button("Go to upload", type="primary", width="stretch"):
        goto("Home")


# =============================================================================== pages
REPEATS = {"Remove them": (True, "Exact repeats go to a “Removed rows” sheet"),
           "Keep them": (False, "Every row stays; repeats are flagged")}


def page_home() -> None:
    render('<div class="dp-hero center"><span class="dp-eyebrow"><i>LOCAL</i> Runs on this computer · no paid APIs</span>'
           '<h1>DocuPilot <span class="grad">AI</span></h1>'
           '<p>Turn any PDF, scan or photo into a clean Excel workbook: tables found automatically, joined across pages, '
           'checked for wrong totals, and ready to use.</p></div>')

    with st.container(key="upload_panel"):
        up = st.file_uploader("Drop a PDF, scan or photo here, or click to choose one",
                              type=[e.lstrip(".") for e in SETTINGS.allowed_extensions], key="uploader",
                              label_visibility="collapsed")
        with st.container(key="opts_row"):
            o1, o2 = st.columns(2, gap="large", vertical_alignment="bottom")
        with o1:
            render(f'<div class="dp-optlabel">{icon("rows")}<span>Repeated rows</span></div>')
            rep = st.radio("Repeated rows", list(REPEATS), captions=[c for _, c in REPEATS.values()],
                           key="opt_repeats", horizontal=True, label_visibility="collapsed")
        with o2:
            # OCR is decided per page by the engine: pictures, garbled text layers and rotated pages are read with OCR,
            # and mixed pages are read both ways with the better tables kept. No user choice needed.
            render(f'<div class="dp-auto">{icon("scan")}<div><b>Scanned pages are handled automatically</b>'
                   '<span>Pages that are pictures, have an unreadable text layer or are rotated are read with OCR; '
                   'pages with real text are read directly.</span></div></div>')

        name, data, start = None, None, False
        if up is not None:
            name, data = up.name, up.getvalue()
        with st.container(horizontal=True, vertical_alignment="center", gap="small", key="samples_row"):
            render('<span class="dp-samples-label">or try a sample:</span>')
            for label, (path, ic, tip) in SAMPLES.items():
                if st.button(label, key=f"sample_{path.stem}", icon=ic, help=tip, type="tertiary",
                             disabled=not path.exists()):
                    name, data, start = path.name, path.read_bytes(), True

        proc_holder = st.empty()
        if up is not None:
            kind, pages = doc_info_preview(name, data)
            over = pages is not None and pages > SETTINGS.max_pages
            pages_txt = f"{pages} page{'s' if pages != 1 else ''}" if pages else "page count unknown"
            render(f'<div class="dp-file"><div class="ic">{icon("file")}</div>'
                   f'<div class="meta"><b>{esc(sanitize_filename(name))}</b>'
                   f'<span>{kind} · {len(data) / 1024 / 1024:,.1f} MB · {pages_txt}</span></div>'
                   + (chip(f"First {SETTINGS.max_pages} pages will be converted", "warning", "alert") if over else "")
                   + "</div>")
            start = st.button("Convert to Excel", type="primary", icon=":material/table_view:") or start
        if start:
            with proc_holder.container(key="process_panel"):
                result = run_processing(name, data, "auto", REPEATS[rep][0], st.empty())
            if result is not None:
                ss.results[result.file_hash] = result
                ss.active = result.file_hash
                ss.applied = None
                record_run(result, data)
                st.rerun()

    res = active_result()
    if res is None:
        kept = ("Each conversion — the uploaded file, its Excel workbook and validation report — is kept in History "
                "on this computer, where you can reopen, download or delete it."
                if SETTINGS.history_enabled and SETTINGS.history_files else
                "The processing history stores metrics only — never document contents.")
        render(note(f"Files are processed on this computer. Nothing is uploaded to external services. {kept}", "", "lock"))
        return
    render_results(res)


def render_results(res: ProcessingResult) -> None:
    m = res.metrics()
    stem = Path(res.file_name).stem
    with st.container(key="ready_panel"):
        c1, c2 = st.columns([3, 1.2], vertical_alignment="center")
        with c1:
            render(f'<h2 class="dp-display">Your Excel file is ready</h2>'
                   f'<p class="dp-sub">{esc(res.file_name)} · read in {res.elapsed:.1f} s</p>')
        with c2:
            st.download_button("Download .xlsx", res.excel_bytes, file_name=f"{stem}_docupilot.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               type="primary", width="stretch", icon=":material/download:", key="dl_ready")
        render('<div class="dp-stats">' + "".join([
            stat("Pages", m["pages"]), stat("Read by OCR", m["ocr_pages"]), stat("Tables", m["tables"]),
            stat("Rows", f"{m['rows']:,}"), stat("Errors found", m["critical"], "bad" if m["critical"] else "good"),
            stat("Warnings", m["warnings"], "warn" if m["warnings"] else "good"),
        ]) + "</div>")
        notes = []
        if res.pages_total:
            notes.append(f"The file has {res.pages_total} pages; the first {m['pages']} were converted.")
        if res.removed_rows:
            notes.append(f"{len(res.removed_rows)} repeated or blank row(s) were removed — they are listed on the "
                         "“Removed rows” sheet.")
        if "totals_separate" in res.options and any(mt.kind == "summary" for t in res.tables for mt in t.row_meta):
            notes.append("Total rows were moved to their own “Totals” sheet.")
        notes += [w for w in res.warnings if "only the first" not in w]
        if notes:
            render('<ul class="dp-notes">' + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>")
        render(page_strip(res.page_reports))

    with st.container(key="preview_panel"):
        render('<h3 class="dp-display">Preview</h3>')
        if not res.tables:
            render('<p class="dp-sub">No table was found in this file.</p>')
        for t in res.tables:
            render(preview_table(t, res.issues))

    with st.container(key="checks_panel"):
        render('<h3 class="dp-display">Checks and problems</h3>')
        summary = check_summary(res.issues)
        if summary:
            render(check_grid(summary))
            looked, ok = sum(c["checked"] for c in summary), sum(c["passed"] for c in summary)
            render(f'<p class="dp-hint">{ok} of {looked} automatic checks passed. Cells that failed are coloured in '
                   'the preview and in the Excel file.</p>')
        problems = sorted((i for i in res.issues if i.severity in ("critical", "warning")),
                          key=lambda i: (i.severity != "critical", i.table_id or "", i.row_index or 0))
        if not problems:
            render('<p class="dp-ok">✓ No problems found. Every check passed.</p>')
        else:
            render('<ul class="dp-lines">' + "".join(issue_line(i) for i in problems[:8]) + "</ul>")
            if len(problems) > 8:
                with st.expander(f"Show all {len(problems)}"):
                    render('<ul class="dp-lines">' + "".join(issue_line(i) for i in problems[8:]) + "</ul>")
        if st.button("Open the Validation Center", type="tertiary", icon=":material/arrow_forward:"):
            goto("Validation Center")

    cleanup_checklist(res)


def _fix_key(res: ProcessingResult, oid: str) -> str:
    return f"fix_{res.file_hash[:12]}_{oid}"


def _tick_suggested(res: ProcessingResult, ids: list[str]) -> None:
    for oid in ids:
        ss[_fix_key(res, oid)] = True


def cleanup_checklist(res: ProcessingResult) -> None:
    with st.container(key="checklist_panel"):
        render('<h3 class="dp-display">Common problems when moving a PDF into Excel</h3>'
               '<p class="dp-sub">Tick what applies to your file and press <b>Apply</b>: the Excel file is rebuilt at once '
               '(the document is not read again). The count shows how many places in <em>your</em> document have each '
               'problem.</p>')
        for o in cleanup.OPTIONS:
            ss.setdefault(_fix_key(res, o.id), o.id in res.options)
        picked = {o.id for o in cleanup.OPTIONS if ss[_fix_key(res, o.id)]}
        suggested = [o.id for o in cleanup.OPTIONS if res.checklist.get(o.id, {}).get("suggested") and o.id not in picked]
        if suggested:
            st.button(f"Tick the {len(suggested)} suggested fix{'es' if len(suggested) != 1 else ''} for this file",
                      on_click=_tick_suggested, args=(res, suggested), icon=":material/auto_fix_high:", key="tick_suggested")
        for section_name in cleanup.SECTIONS:
            render(f'<div class="dp-legend-title">{esc(section_name)}</div>')
            for o in (x for x in cleanup.OPTIONS if x.section == section_name):
                found = res.checklist.get(o.id, {"count": 0, "suggested": False, "examples": []})
                with st.container(key=f"fixrow_{o.id}"):
                    st.checkbox(o.label, key=_fix_key(res, o.id))
                    badges = (chip("Suggested", "lamp") if found["suggested"] else "") + (
                        chip(f"{found['count']:,} found", "ghost") if found["count"] else chip("✓ none found", "passed"))
                    ex = (f'<span class="ex">e.g. {"  ·  ".join(esc(e) for e in found["examples"])}</span>'
                          if found["examples"] else "")
                    render(f'<div class="dp-fix">{badges}<span class="help">{esc(o.help)}</span>{ex}</div>')
        dirty = picked != set(res.options)
        a1, a2 = st.columns([1, 2.4], vertical_alignment="center")
        if a1.button("Apply to the Excel file", type="primary", disabled=not dirty, width="stretch", key="apply_fixes"):
            new = rebuild(res, picked)
            update_run(new)
            ss.results[res.file_hash] = new
            ss.applied = new.rebuild_seconds
            st.rerun()
        if dirty:
            a2.markdown('<span class="dp-hint">You changed the ticks — press Apply to update the file.</span>',
                        unsafe_allow_html=True)
        elif ss.get("applied") is not None:
            a2.markdown(f'<span class="dp-ok">✓ Excel file updated in {ss.applied:.2f} s</span>', unsafe_allow_html=True)


def page_overview(res: ProcessingResult) -> None:
    m = res.metrics()
    right = chip(DOC_TYPES.get(res.doc_type, res.doc_type), "violet", "file") + chip(f"{m['elapsed']:.1f}s", "ghost", "clock")
    if res.ocr_pages:
        right += chip(f"OCR · {len(res.ocr_pages)} page(s)", "cyan", "scan")
    if res.llm_used:
        right += chip(f"LLM · {res.llm_model}", "violet", "sparkles")
    render(page_title(res.file_name, f"Processed {m['pages']} page(s) locally · SHA-256 {res.file_hash[:12]}…", right))
    if m["failed_pages"]:
        render(note(f"<b>{m['failed_pages']} page(s) could not be processed:</b> "
                    f"{', '.join(map(str, res.failed_pages))}. The other pages were processed normally.", "err", "octagon"))
    for w in res.warnings:
        render(note(esc(w), "warn", "alert"))
    render(kpi_grid([
        kpi("Pages processed", m["pages"], "pages", f"{m['failed_pages']} failed" if m["failed_pages"] else "all pages read"),
        kpi("Tables detected", m["tables"], "table", f"{len(res.merge_decisions)} page boundaries evaluated", "cyan"),
        kpi("Rows extracted", f"{m['rows']:,}", "rows", "data rows (summary rows excluded)"),
        kpi("Columns identified", m["columns"], "columns", f"widest table: {m['max_columns']} columns", "cyan"),
        kpi("Validation errors", m["critical"], "octagon", "confirmed discrepancies", "critical"),
        kpi("Warnings", m["warnings"], "alert", "suspected issues for review", "warning"),
        kpi("Processing time", f"{m['elapsed']:.1f}", "clock", "end-to-end on this machine", "", "s"),
        kpi("OCR usage", m["ocr_pages"], "scan",
            f"of {m['pages']} pages" + (f" · mean conf. {sum(p.ocr_confidence for p in res.page_reports if p.ocr_confidence) / max(1, sum(1 for p in res.page_reports if p.ocr_confidence)):.1%}" if m["ocr_pages"] else ""),
            "passed" if m["ocr_pages"] else ""),
    ]))
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        render(section("Findings by severity", "shield"))
        st.altair_chart(charts.severity_chart(res), width="stretch")
    with c2:
        render(section("Rows by source page", "chart"))
        if res.tables:
            st.altair_chart(charts.rows_by_page_chart(res), width="stretch")
        else:
            render(empty_state("No tables", "No table rows were extracted.", "table"))

    render(section("Page-by-page processing", "pages"))
    rows = []
    for p in res.page_reports:
        st_kind = {"ok": "passed", "no_tables": "info", "warning": "warning", "failed": "critical"}.get(p.status, "ghost")
        st_label = {"ok": "Tables found", "no_tables": "No tables", "warning": "Review", "failed": "Failed"}.get(p.status, p.status)
        rows.append([f"<b>{p.page}</b>", esc(p.kind.title()), esc(p.method or "—"),
                     f"{p.ocr_confidence:.1%}" if p.ocr_confidence else "—", p.tables_found,
                     chip(st_label, st_kind), esc("; ".join(p.warnings)) or '<span style="color:var(--muted)">—</span>'])
    render('<div class="dp-card" style="padding:6px 8px">' +
           html_table(["Page", "Classification", "Method", "OCR conf.", "Tables", "Status", "Notes"], rows, num_cols=(4,)) + "</div>")

    c1, c2 = st.columns([1.15, 1], gap="medium")
    with c1:
        render(section("Tables", "table"))
        trows = []
        for t in res.tables:
            pages = f"{t.pages[0]}–{t.pages[-1]}" if len(t.pages) > 1 else str(t.pages[0])
            trows.append([f"<b>{t.table_id}</b>", esc(t.title or "Untitled"), pages, len(t.data_row_indices()), t.ncols,
                          chip("merged", "cyan", "merge") if len(t.pages) > 1 else chip("single page", "ghost")])
        render('<div class="dp-card" style="padding:6px 8px">' +
               (html_table(["ID", "Title", "Pages", "Rows", "Cols", ""], trows, num_cols=(3, 4)) if trows
                else '<p style="color:var(--muted);padding:12px">No tables detected.</p>') + "</div>")
    with c2:
        render(section("Values found outside tables", "sparkles"))
        frows = [[esc(f.label), f"{f.value:,}" if isinstance(f.value, Decimal) else esc(f.value), f.page] for f in res.fields]
        render('<div class="dp-card" style="padding:6px 8px">' +
               (html_table(["Label", "Value", "Page"], frows, num_cols=(1, 2)) if frows
                else '<p style="color:var(--muted);padding:12px">No totals or balances were found outside the tables.</p>') + "</div>")
    with st.expander("Stage timings"):
        st.altair_chart(charts.stage_chart(res), width="stretch")
    if res.llm_notes:
        with st.expander("Local LLM notes"):
            for n in res.llm_notes:
                st.write("• " + n)


def page_tables(res: ProcessingResult) -> None:
    render(page_title("Table Explorer", "Search, sort and inspect every extracted row with its source page and findings."))
    if not res.tables:
        render(empty_state("No tables detected", "The document did not contain recognisable tables.", "table"))
        return
    labels = {f"{t.table_id} · {t.title or 'Untitled'} · {len(t.data_row_indices())} rows × {t.ncols} cols": t for t in res.tables}
    t = labels[st.selectbox("Table", list(labels), label_visibility="collapsed")]
    issue_cells = issue_cells_for(res, t)
    pages = f"pages {t.pages[0]}–{t.pages[-1]}" if len(t.pages) > 1 else f"page {t.pages[0]}"
    chips_html = chip(pages, "violet", "pages") + chip(", ".join(t.methods), "ghost", "scan")
    if len(t.pages) > 1:
        chips_html += chip(f"merged from {len(t.segments)} fragments", "cyan", "merge")
    if t.repeated_headers_removed:
        chips_html += chip(f"{len(t.repeated_headers_removed)} repeated header(s) removed", "info")
    flagged_rows = {r for (r, _c) in issue_cells}
    if flagged_rows:
        chips_html += chip(f"{len(flagged_rows)} row(s) with findings", "warning", "alert")
    render(f'<div class="dp-chips" style="margin:2px 0 12px">{chips_html}</div>')

    df = table_frame(t, issue_cells)
    data_cols = [c for c in df.columns if c not in ("Row ID", "Page", "Flags")]
    f1, f2, f3, f4 = st.columns([2.2, 2.4, 1.2, 1.2])
    q = f1.text_input("Search", placeholder="Search all cells…", label_visibility="collapsed")
    show_cols = f2.multiselect("Columns", data_cols, default=data_cols, label_visibility="collapsed",
                               placeholder="Columns to show")
    only_flagged = f3.toggle("Only flagged", value=False)
    show_meta = f4.toggle("Provenance", value=True, help="Show Row ID, source page and finding count")
    page_sel = None
    if len(t.pages) > 1:
        page_sel = st.pills("Source pages", t.pages, selection_mode="multi", default=t.pages, key=f"pages_{t.table_id}")

    mask = pd.Series(True, index=df.index)
    if q:
        ql = q.lower()
        mask &= df.apply(lambda r: any(ql in str(v).lower() for v in r.values if v is not None), axis=1)
    if only_flagged:
        mask &= df["Flags"] > 0
    if page_sel is not None:
        mask &= df["Page"].isin(page_sel or [])
    view_cols = (["Row ID", "Page"] if show_meta else []) + (show_cols or data_cols) + (["Flags"] if show_meta else [])
    view = df.loc[mask, view_cols]

    col_index = {c: i for i, c in enumerate(data_cols)}
    colours = {"critical": "background-color: #ffe4e8; color: #9f1239; font-weight: 600",
               "warning": "background-color: #fef3c7; color: #92400e; font-weight: 600"}

    def highlight(frame: pd.DataFrame) -> pd.DataFrame:
        css = pd.DataFrame("", index=frame.index, columns=frame.columns)
        for (r, c), iss in issue_cells.items():
            if r not in frame.index:
                continue
            sev = "critical" if any(i.severity == "critical" for i in iss) else "warning"
            if c is None:
                if "Row ID" in frame.columns:
                    css.loc[r, "Row ID"] = colours[sev]
                continue
            name = data_cols[c] if c < len(data_cols) else None
            if name in frame.columns:
                css.loc[r, name] = colours[sev]
        for r in frame.index:
            if t.row_meta[r].kind == "summary":
                css.loc[r, :] = css.loc[r, :].where(css.loc[r, :] != "", "font-weight: 700; color: #4338ca")
        return css

    config: dict[str, Any] = {"Row ID": st.column_config.TextColumn("Row ID", width="small", pinned=True),
                              "Page": st.column_config.NumberColumn("Page", width="small"),
                              "Flags": st.column_config.NumberColumn("Flags", width="small", help="Findings on this row")}
    # Display formats mirror the source precision; the underlying values stay numeric so sorting is numeric.
    formatters: dict[str, Any] = {"Page": "{:d}", "Flags": "{:d}"}
    for i, c in enumerate(data_cols):
        ty = t.col_types[i] if i < len(t.col_types) else "text"
        if c not in view.columns:
            continue
        if ty in ("number", "integer", "percent") and pd.api.types.is_numeric_dtype(df[c]):
            decs = [max(0, -v.as_tuple().exponent) for v in (row[i] for row in t.values) if isinstance(v, Decimal)]
            d = min(max(decs, default=2), 4)
            formatters[c] = f"{{:.{max(d - 2, 0)}%}}" if ty == "percent" else f"{{:,.{d}f}}"
        elif ty == "date":
            formatters[c] = lambda v: v.isoformat() if isinstance(v, date) else ("" if v is None else str(v))
    styler = view.style.apply(highlight, axis=None).format(
        {k: v for k, v in formatters.items() if k in view.columns}, na_rep="")
    st.caption(f"Showing {len(view):,} of {len(df):,} rows · {len(view_cols)} columns · click a header to sort · "
               "highlighted cells have findings (red = confirmed, amber = suspected)")
    st.dataframe(styler, width="stretch", hide_index=True, column_config=config,
                 height=min(560, 38 + 35 * max(len(view), 1)))

    tabs = st.tabs(["Row inspector", "Merges & structure", "Duplicates & missing", "Warnings & header mapping"])
    with tabs[0]:
        ids = list(df.loc[mask, "Row ID"]) or list(df["Row ID"])
        default = next((i for i, rid in enumerate(ids) if df.loc[df["Row ID"] == rid, "Flags"].iloc[0] > 0), 0)
        rid = st.selectbox("Row", ids, index=default, key=f"inspect_{t.table_id}")
        ri = int(df.index[df["Row ID"] == rid][0])
        meta = t.row_meta[ri]
        c1, c2 = st.columns([1.3, 1], gap="medium")
        with c1:
            rows = []
            for ci, (name, raw, val) in enumerate(zip(t.columns, t.rows[ri], t.values[ri])):
                flags = issue_cells.get((ri, ci), [])
                fl = "".join(chip(SEVERITY_LABEL[i.severity], i.severity) for i in flags)
                typed = "—" if val is None else (f"{val:,}" if isinstance(val, Decimal) else esc(val))
                rows.append([f"<b>{esc(name)}</b>", esc(raw) if raw else '<span style="color:var(--muted)">empty</span>',
                             typed, esc(t.col_types[ci]), fl])
            render('<div class="dp-card" style="padding:6px 8px">' +
                   html_table(["Column", "Extracted text", "Typed value", "Type", "Findings"], rows) + "</div>")
        with c2:
            render(f'<div class="dp-card"><div class="dp-chips" style="margin-bottom:10px">{chip(f"Page {meta.page}", "violet", "pages")}'
                   f'{chip(meta.method, "ghost", "scan")}{chip(meta.kind, "cyan")}{chip(meta.segment, "ghost")}</div>'
                   f'<div style="color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em">Operations</div>'
                   + ("".join(f'<div style="color:var(--text-2);font-size:.86rem;margin:4px 0">• {esc(o)}</div>' for o in meta.ops)
                      or '<div style="color:var(--muted);font-size:.86rem;margin:4px 0">No normalisation needed</div>') + "</div>")
            for iss in [i for i in res.issues if i.table_id == t.table_id and i.row_index == ri]:
                render(issue_card(iss))
    with tabs[1]:
        if t.merge_decisions:
            for d in t.merge_decisions:
                kind = "passed" if d.merged else "info"
                render(f'<div class="dp-issue {kind}"><div class="hd"><span class="title">{esc(d.left)} → {esc(d.right)}</span>'
                       f'{chip("Merged" if d.merged else "Kept separate", kind, "merge")}{chip(f"evidence score {d.score:g}", "ghost")}'
                       f'{chip(d.uncertainty, "warning") if d.uncertainty else ""}</div>'
                       + "".join(f'<div class="ex">• {esc(e)}</div>' for e in d.evidence) + "</div>")
        else:
            render(note("This table did not cross a page boundary, so no merge decision was needed.", "", "info"))
        render(section("Page fragments", "pages"))
        render(f'<div class="dp-chips">{"".join(chip(s, "violet") for s in t.segments)}</div>')
        render(section("Repeated headers removed", "rows"))
        if t.repeated_headers_removed:
            for rh in t.repeated_headers_removed:
                render(note(f"Page {rh['page']}: {rh['count']} header row(s) removed ({esc(rh['where'])}). Data rows untouched.", "", "check"))
        else:
            render('<p style="color:var(--muted)">None.</p>')
    with tabs[2]:
        dup = [i for i in res.issues if i.table_id == t.table_id and i.check in ("duplicate_rows", "boundary_duplicate")]
        miss = [i for i in res.issues if i.table_id == t.table_id and i.check in ("missing_value", "sequence_gap", "data_type", "column_shift")]
        render(section("Duplicate-looking rows", "rows"))
        for i in dup or []:
            render(issue_card(i))
        if not dup:
            render('<p style="color:var(--muted)">None found.</p>')
        render(section("Missing, suspicious or misaligned values", "alert"))
        for i in miss[:60]:
            render(issue_card(i))
        if not miss:
            render('<p style="color:var(--muted)">None found.</p>')
    with tabs[3]:
        for w in t.warnings:
            render(note(esc(w), "warn", "alert"))
        if not t.warnings:
            render(note("No extraction warnings for this table.", "", "check"))
        render(section("Header normalisation", "wand"))
        hrows = [[esc(o), f"<b>{esc(n)}</b>", esc(r or "—"), esc(ty)] for o, n, r, ty in
                 zip(t.original_header, t.columns, t.roles, t.col_types)]
        render('<div class="dp-card" style="padding:6px 8px">' +
               html_table(["Original header", "Normalised", "Semantic role", "Type"], hrows) + "</div>")
        for op in t.header_ops:
            st.caption("• " + op)


def page_validation(res: ProcessingResult) -> None:
    render(page_title("Validation Center", "Every check returns Passed, Failed or Unable to Verify. Values are never altered to make totals match."))
    counts = {s: sum(1 for i in res.issues if i.severity == s) for s in ("critical", "warning", "info", "passed")}
    render('<div class="dp-states">' + "".join([
        state_card("critical", counts["critical"], "confirmed discrepancies"),
        state_card("warning", counts["warning"], "suspected — needs review"),
        state_card("info", counts["info"], "informational / unable to verify"),
        state_card("passed", counts["passed"], "checks satisfied"),
    ]) + "</div>")
    st.write("")
    f1, f2, f3, f4 = st.columns([2.4, 1.2, 1.4, 1.2])
    sev_names = {"Critical": "critical", "Warning": "warning", "Informational": "info", "Passed": "passed"}
    chosen = f1.pills("Severity", list(sev_names), selection_mode="multi",
                      default=[k for k, v in sev_names.items() if counts[v] and v != "passed"] or ["Passed"],
                      label_visibility="collapsed")
    tables = ["All tables"] + [t.table_id for t in res.tables]
    tsel = f2.selectbox("Table", tables, label_visibility="collapsed")
    checks = sorted({i.check for i in res.issues})
    csel = f3.selectbox("Check", ["All checks"] + checks, label_visibility="collapsed",
                        format_func=lambda c: CHECK_LABELS.get(c, c))
    conf = f4.selectbox("Certainty", ["All", "Confirmed", "Suspected"], label_visibility="collapsed")
    wanted = {sev_names[c] for c in (chosen or [])}
    items = [i for i in res.issues if i.severity in wanted
             and (tsel == "All tables" or i.table_id == tsel)
             and (csel == "All checks" or i.check == csel)
             and (conf == "All" or (conf == "Confirmed") == bool(i.confirmed))]
    order = {"critical": 0, "warning": 1, "info": 2, "passed": 3}
    items.sort(key=lambda i: (order[i.severity], i.table_id or "", i.row_index if i.row_index is not None else -1))
    if not items:
        render(empty_state("Nothing matches these filters",
                           "Adjust the filters above. If no critical issues are listed, none were detected — "
                           "checks that could not run are under Informational.", "shield"))
        return
    st.caption(f"{len(items)} finding(s)" + (" · showing first 150" if len(items) > 150 else ""))
    c1, c2 = st.columns(2, gap="medium")
    for k, i in enumerate(items[:150]):
        (c1 if k % 2 == 0 else c2).markdown(issue_card(i), unsafe_allow_html=True)


def page_export(res: ProcessingResult) -> None:
    render(page_title("Export Center", "A formatted workbook with your data, the validation report and a full audit trail."))
    stem = Path(res.file_name).stem
    n_rows = sum(len(t.rows) for t in res.tables)
    sheets = [("Extracted Data", f"{len(res.tables)} table(s), {n_rows} rows · typed values, filters, frozen header", "table"),
              ("Validation Report", f"{len(res.issues)} check results · expected vs. extracted · severity · status", "shield"),
              ("Extraction Audit", f"{n_rows} rows · source page, method, operations, warnings", "eye"),
              ("Document Summary", "pages, merge decisions, header mapping, fields", "file")]
    if len(res.tables) > 1:
        sheets += [(f"{t.table_id} sheet", f"{len(t.rows)} rows × {t.ncols} cols · own filter & frozen header", "columns")
                   for t in res.tables]
    c1, c2 = st.columns([1.4, 1], gap="large")
    with c1:
        render(section("Workbook contents", "table"))
        rows = [[f'<span style="color:var(--indigo)">{icon(ic)}</span>'.replace("<svg", '<svg width="16" height="16"'),
                 f"<b>{esc(n)}</b>", esc(d)] for n, d, ic in sheets]
        render('<div class="dp-card" style="padding:6px 8px">' + html_table(["", "Sheet", "Contents"], rows) + "</div>")
    with c2:
        render(section("Download", "download"))
        render(f'<div class="dp-card glow"><div style="font-weight:700;font-size:1.05rem;color:var(--text)">{esc(stem)}_docupilot.xlsx</div>'
               f'<div style="color:var(--muted);font-size:.85rem;margin:4px 0 14px">{len(res.excel_bytes) / 1024:,.0f} KB · '
               f'{len(res.tables)} table(s) · {sum(1 for i in res.issues if i.severity == "critical")} critical finding(s)</div></div>')
        st.download_button("Download Excel workbook", res.excel_bytes, file_name=f"{stem}_docupilot.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary",
                           width="stretch", icon=":material/download:")
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            for t in res.tables:
                frame = pd.DataFrame([[to_display(v) for v in r] for r in t.values], columns=t.columns)
                frame.insert(0, "Source Page", [m.page for m in t.row_meta])
                frame.insert(0, "Row ID", [m.row_id for m in t.row_meta])
                z.writestr(f"{t.table_id}.csv", frame.to_csv(index=False))
        st.download_button("Tables as CSV (.zip)", zbuf.getvalue(), file_name=f"{stem}_tables.zip", mime="application/zip",
                           width="stretch", disabled=not res.tables)
        report = [{k: (v if not isinstance(v, Decimal) else float(v)) for k, v in i.__dict__.items()} for i in res.issues]
        st.download_button("Validation report (.json)", json.dumps(report, indent=2, default=str), file_name=f"{stem}_validation.json",
                           mime="application/json", width="stretch")
    render(section("Preview", "eye"))
    tabs = st.tabs(["Validation Report", "Extraction Audit"])
    with tabs[0]:
        vr = pd.DataFrame([{"Check": CHECK_LABELS.get(i.check, i.check), "Severity": SEVERITY_LABEL[i.severity], "Status": i.status,
                            "Table": i.table_id, "Row": i.row_id, "Page": i.page, "Column": i.column, "Extracted": i.extracted,
                            "Expected": i.expected, "Difference": i.difference, "Explanation": i.explanation} for i in res.issues])
        for col in ("Row", "Column", "Extracted", "Expected", "Difference"):
            if col in vr:
                vr[col] = vr[col].map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else
                                      (f"{v:,.2f}" if isinstance(v, float) else str(v)))
        st.dataframe(vr, width="stretch", hide_index=True, height=380)
    with tabs[1]:
        au = pd.DataFrame([{"Page": m.page, "Table": t.table_id, "Row ID": m.row_id, "Type": m.kind, "Method": m.method,
                            "Operations": "; ".join(m.ops) or "none"} for t in res.tables for m in t.row_meta])
        st.dataframe(au, width="stretch", hide_index=True, height=380)


HISTORY_PAGE_SIZE = 10
FILE_MIME = {"workbook": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "validation": "application/json"}


def _open_run(entry: dict) -> None:
    res = load_result(entry)
    if res is None:
        st.toast("This run's saved result is missing or was changed on disk, so it cannot be reopened.", icon="⚠️")
        return
    ss.results[res.file_hash] = res
    ss.active = res.file_hash
    ss.applied = None
    goto("Home")


def page_history() -> None:
    hist = load_history()
    stored = SETTINGS.history_files
    render(page_title("History", "Every document converted on this computer, with the files it produced. "
                      "Reopen a result, download its files again, or delete it."
                      if stored else "Metrics of documents processed on this computer. File storage is switched off "
                      "(DOCUPILOT_HISTORY_FILES=0), so only metrics are kept."))
    if not hist:
        render(empty_state("No history yet", "Convert a document and it will be listed here with its files.", "history"))
        return

    totals = {"runs": len(hist), "pages": sum(int(e.get("pages", 0)) for e in hist),
              "rows": sum(int(e.get("rows", 0)) for e in hist)}
    render('<div class="dp-stats four">' + "".join([
        stat("Conversions", totals["runs"]), stat("Pages read", f"{totals['pages']:,}"),
        stat("Rows extracted", f"{totals['rows']:,}"), stat("Stored on disk", f"{storage_used() / 1024 / 1024:,.1f} MB"),
    ]) + "</div>")

    f1, f2 = st.columns([3, 1], vertical_alignment="center")
    q = f1.text_input("Search", placeholder="Search by file name…", label_visibility="collapsed", key="hist_q")
    runs = [e for e in reversed(hist) if not q or q.lower() in e.get("file_name", "").lower()]
    pages = max(1, -(-len(runs) // HISTORY_PAGE_SIZE))
    page_no = f2.selectbox("Page", range(1, pages + 1), format_func=lambda p: f"Page {p} of {pages}", key="hist_page",
                           label_visibility="collapsed") if pages > 1 else 1
    if not runs:
        render(empty_state("Nothing matches", "No converted file name contains that text.", "history"))

    for k, e in enumerate(runs[(page_no - 1) * HISTORY_PAGE_SIZE: page_no * HISTORY_PAGE_SIZE]):
        rid = e.get("run_id") or f"legacy{k}_{e.get('timestamp', '')}"
        files = run_files(e)
        with st.container(key=f"run_{re.sub(r'[^A-Za-z0-9_-]', '_', rid)}"):
            crit, warn = int(e.get("critical", 0)), int(e.get("warnings", 0))
            chips = (chip(DOC_TYPES.get(e.get("doc_type"), e.get("doc_type") or "—"), "violet", "file")
                     + chip(f"{e.get('pages', 0)} page(s)", "ghost", "pages")
                     + chip(f"{e.get('tables', 0)} table(s) · {int(e.get('rows', 0)):,} rows", "ghost", "table")
                     + (chip(f"OCR on {e['ocr_pages']}", "cyan", "scan") if e.get("ocr_pages") else "")
                     + chip(f"{crit} error(s)", "critical" if crit else "passed", "octagon" if crit else "check")
                     + (chip(f"{warn} warning(s)", "warning", "alert") if warn else "")
                     + (chip(f"{e['removed_rows']} row(s) removed", "ghost") if e.get("removed_rows") else ""))
            when = e.get("timestamp", "")
            if e.get("updated"):
                when += f" · clean-up re-applied {e['updated'][11:16]}"
            render(f'<div class="dp-run"><div class="ic">{icon("file")}</div><div class="meta">'
                   f'<b>{esc(e.get("file_name", ""))}</b><span>{esc(when)} · read in {float(e.get("elapsed", 0)):.1f} s'
                   f'</span></div></div><div class="dp-chips" style="margin:8px 0 10px">{chips}</div>')
            if not files:
                render('<p class="dp-hint">Only metrics were kept for this run (it was converted before file storage '
                       'was switched on, or its files were removed).</p>')
                continue
            with st.container(horizontal=True, gap="small", vertical_alignment="center", key=f"acts_{rid}"):
                if "result" in files and st.button("Open results", key=f"open_{rid}", type="primary",
                                                   icon=":material/visibility:"):
                    _open_run(e)
                if "source" in files:
                    st.download_button("Original file", files["source"].read_bytes(), file_name=files["source"].name,
                                       key=f"src_{rid}", icon=":material/description:")
                if "workbook" in files:
                    st.download_button("Excel workbook", files["workbook"].read_bytes(),
                                       file_name=files["workbook"].name, mime=FILE_MIME["workbook"], key=f"xlsx_{rid}",
                                       icon=":material/table_view:")
                if "validation" in files:
                    st.download_button("Validation report", files["validation"].read_bytes(),
                                       file_name=files["validation"].name, mime=FILE_MIME["validation"],
                                       key=f"json_{rid}", icon=":material/fact_check:")
                if e.get("run_id") and st.button("Delete", key=f"del_{rid}", type="tertiary", icon=":material/delete:",
                                                 help="Remove this run and all its stored files"):
                    delete_run(e["run_id"])
                    st.rerun()

    st.write("")
    with st.expander("Clear the whole history"):
        st.caption("Deletes every entry and every stored file (uploaded documents, workbooks, reports). This cannot be undone.")
        if st.button("Delete all history", icon=":material/delete_forever:", key="clear_all"):
            clear_history()
            st.rerun()


# =============================================================================== router
page = ss.nav
res = active_result()
if page == "Home":
    page_home()
elif page == "History":
    page_history()
elif res is None:
    render(page_title(page))
    no_result_state({"Overview": "the results dashboard", "Table Explorer": "extracted tables",
                     "Validation Center": "validation findings", "Export Center": "export options"}[page])
elif page == "Overview":
    page_overview(res)
elif page == "Table Explorer":
    page_tables(res)
elif page == "Validation Center":
    page_validation(res)
elif page == "Export Center":
    page_export(res)
