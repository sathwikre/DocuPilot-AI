"""Central orchestration: inspection -> classification -> text/OCR -> table detection ->
multi-page reconstruction -> validation -> Excel.

The orchestrator makes the recovery decisions:
  * scanned / garbled / rotated pages go to OCR;
  * a digital page that yields no usable table but looks image-heavy is retried with OCR;
  * malformed ruled tables fall back to layout reconstruction (inside pdf_extractor);
  * a failing page is recorded and skipped while the remaining pages continue;
  * schema mismatches block table merges (inside table_merger);
  * validation discrepancies are reported, never "fixed".
Progress callbacks report real work done (stage, page counters, elapsed time).
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from config.settings import SETTINGS
from core import ocr_engine
from core.data_normalizer import type_table
from core.document_inspector import inspect_document, open_document, pdf_bytes_for
from core.excel_exporter import build_workbook
from core.field_extractor import dedupe_fields, detect_doc_type, fields_from_lines, fields_from_summary_rows
from core.models import DocumentRejected, PageReport, ProcessingResult, RawTable, Word
from core.pdf_extractor import extract_digital_page, open_plumber, page_words
from core.table_detector import words_text
from core.table_merger import build_logical_tables
from core.validator import Validator
from core.value_parser import normalise_text
from utils.file_validation import sha256_bytes, validate_upload
from utils.logging_config import get_logger, log_event

log = get_logger("orchestrator")

STAGES = [
    "Document inspection",
    "Page classification",
    "Text extraction / OCR",
    "Table detection",
    "Multi-page reconstruction",
    "Data validation",
    "Excel generation",
]


@dataclass
class ProgressEvent:
    stage_index: int
    stage: str
    detail: str
    done_units: int = 0
    total_units: int = 0
    elapsed: float = 0.0
    finished: bool = False
    warnings: list[str] = field(default_factory=list)


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class _PageWork:
    """Per-page intermediate state between the text and table stages."""

    report: PageReport
    words: list[Word] = field(default_factory=list)
    ocr: Optional[ocr_engine.OCRWords] = None
    tables: list[RawTable] = field(default_factory=list)
    lines: list[list[Word]] = field(default_factory=list)


class AgentOrchestrator:
    """Runs the full document-to-Excel pipeline."""

    def __init__(self, use_llm: bool = False, progress: Optional[ProgressCallback] = None, ocr_enabled: bool = True):
        self.use_llm = use_llm
        self.progress = progress
        self.ocr_enabled = ocr_enabled
        self._t0 = 0.0
        self._stage_t0 = 0.0
        self.timings: dict[str, float] = {}

    # ------------------------------------------------------------------ progress helpers
    def _emit(self, idx: int, detail: str, done: int = 0, total: int = 0, finished: bool = False, warnings=None) -> None:
        if self.progress:
            try:
                self.progress(ProgressEvent(idx, STAGES[idx], detail, done, total, time.time() - self._t0, finished,
                                            list(warnings or [])))
            except Exception:  # a UI callback must never break processing
                log.exception("progress callback failed")

    def _begin(self, idx: int, detail: str = "", total: int = 0) -> None:
        self._stage_t0 = time.time()
        self._emit(idx, detail or "started", 0, total)

    def _end(self, idx: int, detail: str) -> None:
        self.timings[STAGES[idx]] = round(time.time() - self._stage_t0, 3)
        self._emit(idx, detail, finished=True)

    # ------------------------------------------------------------------ main entry
    def run(self, data: bytes, filename: str) -> ProcessingResult:
        """Process a document. Raises DocumentRejected / FileValidationError for unusable inputs."""
        self._t0 = time.time()
        self.timings = {}
        warnings: list[str] = []

        # 0. Inspection
        self._begin(0, "validating file")
        safe_name, kind = validate_upload(filename, data)
        file_hash = sha256_bytes(data)
        doc = open_document(data, kind)
        try:
            info = inspect_document(doc, safe_name, kind)
            n = info.page_count
            self._end(0, f"{n} page{'s' if n != 1 else ''}, {kind.upper()}")
            log_event(log, "run started", file_hash=file_hash[:12], pages=n, source=kind)

            # 1. Page classification
            self._begin(1, "classifying pages", n)
            works = [_PageWork(PageReport(page=p.number, kind=p.kind, warnings=list(p.notes))) for p in info.pages]
            counts = Counter(p.kind for p in info.pages)
            needs_ocr = [p.number for p in info.pages if p.kind in ("scanned", "mixed") or (p.kind == "digital" and p.rotation)]
            if needs_ocr and not self.ocr_enabled:
                warnings.append("OCR is disabled; scanned pages cannot be read.")
            self._end(1, ", ".join(f"{v} {k}" for k, v in counts.items()))

            pdf_bytes = pdf_bytes_for(doc) if kind == "image" else data
            plumber = open_plumber(pdf_bytes)
            try:
                self._text_stage(doc, plumber, info, works)
                self._table_stage(doc, plumber, info, works)
            finally:
                plumber.close()
        finally:
            doc.close()

        # 4. Multi-page reconstruction + schema/type normalisation
        self._begin(4, "stitching table fragments")
        raws = [t for w in works for t in w.tables]
        running = self._running_text(works)
        tables, decisions = build_logical_tables(raws, running)
        llm = None
        llm_notes: list[str] = []
        if self.use_llm:
            llm = self._make_llm(llm_notes)
        for t in tables:
            type_table(t, llm_mapper=llm.map_headers if llm else None)
        fields = []
        for w in works:
            fields.extend(fields_from_lines(w.lines, w.report.page))
        for t in tables:
            fields.extend(fields_from_summary_rows(t))
        fields = dedupe_fields(fields)
        all_text = " ".join(words_text(ln) for w in works for ln in w.lines) + " " + " ".join(
            " ".join(t.original_header) + " " + (t.title or "") for t in tables)
        first_line = next((words_text(ln) for w in works for ln in w.lines), "")
        doc_type = detect_doc_type(all_text, tables, first_line)
        merged = sum(d.merged for d in decisions)
        self._end(4, f"{len(raws)} fragment(s) -> {len(tables)} table(s); {merged} page merge(s)")
        if llm:
            llm_notes.extend(llm.notes)

        # 5. Validation
        self._begin(5, "running checks")
        reports = [w.report for w in works]
        issues = Validator(SETTINGS.money_tolerance).validate(tables, fields, doc_type, reports)
        sev = Counter(i.severity for i in issues)
        self._end(5, f"{sev.get('critical', 0)} critical, {sev.get('warning', 0)} warnings, {sev.get('passed', 0)} passed")

        result = ProcessingResult(
            file_name=safe_name, file_hash=file_hash, doc_info=info, doc_type=doc_type, page_reports=reports,
            tables=tables, issues=issues, fields=fields, merge_decisions=decisions, stage_timings=self.timings,
            elapsed=0.0, warnings=warnings + info.warnings, llm_used=bool(llm and llm.calls), llm_model=llm.model if llm else None,
            llm_notes=llm_notes,
        )

        # 6. Excel
        self._begin(6, "building workbook")
        result.elapsed = round(time.time() - self._t0, 3)
        result.excel_bytes = build_workbook(result)
        self._end(6, f"{len(result.excel_bytes) / 1024:.0f} KB workbook")
        result.elapsed = round(time.time() - self._t0, 3)
        result.stage_timings = dict(self.timings)
        log_event(log, "run finished", file_hash=file_hash[:12], **{k: v for k, v in result.metrics().items()})
        return result

    # ------------------------------------------------------------------ stages
    def _text_stage(self, doc, plumber, info, works: list[_PageWork]) -> None:
        n = len(works)
        self._begin(2, "reading text", n)
        for i, w in enumerate(works):
            p = info.pages[i]
            t0 = time.time()
            if p.kind in ("scanned", "mixed") or p.rotation:
                self._emit(2, f"running OCR on page {i + 1} of {n}", i, n)
            try:
                if p.kind == "empty":
                    w.report.method = "none"
                elif p.kind in ("scanned", "mixed") or p.rotation:
                    if p.kind == "mixed":
                        w.words = page_words(plumber.pages[i])  # keep the native layer for comparison
                    self._ocr(doc, i, w)
                else:
                    w.words = page_words(plumber.pages[i])
                    w.report.method = "native text"
            except Exception as exc:
                self._fail(w, f"text extraction failed: {type(exc).__name__}: {exc}")
            w.report.elapsed += time.time() - t0
            self._emit(2, f"page {i + 1} of {n}" + (" (OCR)" if w.report.ocr_used else ""), i + 1, n,
                       warnings=w.report.warnings if w.report.status == "failed" else None)
        ocr_n = sum(w.report.ocr_used for w in works)
        self._end(2, f"{n} page(s) read, {ocr_n} with OCR")

    def _ocr(self, doc, i: int, w: _PageWork) -> bool:
        if not self.ocr_enabled or not ocr_engine.ocr_available():
            reason = "OCR disabled" if not self.ocr_enabled else f"OCR engine {ocr_engine.ocr_status()}"
            if not w.words:
                self._fail(w, f"page needs OCR but {reason}")
            else:
                w.report.warnings.append(f"OCR not available ({reason}); using native text only")
            return False
        w.ocr = ocr_engine.ocr_page_words(doc[i], i + 1)
        w.report.ocr_used = True
        w.report.ocr_confidence = w.ocr.mean_confidence
        w.report.method = f"OCR ({ocr_engine.ocr_backend()})"
        w.report.warnings.extend(f"image pre-processing: {o}" for o in w.ocr.ops)
        if not w.ocr.words:
            w.report.warnings.append("OCR found no text on this page")
        return True

    def _table_stage(self, doc, plumber, info, works: list[_PageWork]) -> None:
        n = len(works)
        self._begin(3, "detecting tables", n)
        for i, w in enumerate(works):
            if w.report.status == "failed":
                self._emit(3, f"page {i + 1} skipped (failed earlier)", i + 1, n)
                continue
            p = info.pages[i]
            t0 = time.time()
            try:
                if w.ocr is not None:
                    tables, lines = ocr_engine.tables_from_ocr(w.ocr)
                    if p.kind == "mixed" and w.words:
                        native = extract_digital_page(plumber.pages[i], i + 1, w.words)
                        if self._better(native.tables, tables):
                            w.report.warnings.append("native text layer gave better tables than OCR; used native extraction")
                            tables, lines = native.tables, native.text_lines
                            w.report.method = "native text (mixed page)"
                    w.tables, w.lines = tables, lines
                elif p.kind == "digital":
                    ex = extract_digital_page(plumber.pages[i], i + 1, w.words)
                    w.report.warnings.extend(ex.notes)
                    w.tables, w.lines = ex.tables, ex.text_lines
                    weak = not ex.tables or max(t.quality for t in ex.tables) < 0.5
                    if weak and p.likely_table and p.image_coverage >= 0.3:
                        # Recovery: native layer yields nothing useful but the page carries images -> OCR it.
                        if self._ocr(doc, i, w):
                            tables, lines = ocr_engine.tables_from_ocr(w.ocr)
                            if self._better(tables, w.tables):
                                w.tables, w.lines = tables, lines
                                w.report.warnings.append("native extraction was insufficient; recovered tables with OCR")
                else:
                    w.lines = []
                w.report.tables_found = len(w.tables)
                if w.report.status != "failed":
                    w.report.status = "ok" if w.tables else "no_tables"
                    if w.tables and any(t.quality < 0.6 for t in w.tables):
                        w.report.status = "warning"
                        w.report.warnings.append("a table on this page has low structural consistency")
            except Exception as exc:
                log.exception("table detection failed on page %s", i + 1)
                self._fail(w, f"table detection failed: {type(exc).__name__}: {exc}")
            w.report.elapsed += time.time() - t0
            self._emit(3, f"page {i + 1} of {n}: {len(w.tables)} table fragment(s)", i + 1, n)
        total = sum(len(w.tables) for w in works)
        self._end(3, f"{total} table fragment(s) on {sum(bool(w.tables) for w in works)} page(s)")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _better(a: list[RawTable], b: list[RawTable]) -> bool:
        """True when table list `a` is clearly better than `b`."""
        def score(ts):
            return sum(t.quality * len(t.rows) * max(t.ncols, 1) for t in ts)
        return score(a) > 1.1 * score(b)

    @staticmethod
    def _fail(w: _PageWork, msg: str) -> None:
        w.report.status = "failed"
        w.report.warnings.append(msg)
        w.tables, w.lines = [], []
        log.warning("page %s failed: %s", w.report.page, msg.split(":")[0])

    @staticmethod
    def _running_text(works: list[_PageWork]) -> set[str]:
        """Lines repeated on two or more pages (running headers/footers)."""
        seen: Counter = Counter()
        for w in works:
            page_set = {normalise_text(words_text(ln)).lower() for ln in w.lines}
            page_set |= {normalise_text(t.title).lower() for t in w.tables if t.title}
            seen.update(page_set)  # count each text at most once per page
        return {k for k, v in seen.items() if v >= 2 and k}

    def _make_llm(self, notes: list[str]):
        try:
            from models.local_llm import LocalLLM
            llm = LocalLLM()
            st = llm.status()
            if not st.available:
                notes.append(f"Local LLM requested but unavailable: {st.error}. Continued with rules only.")
                return None
            llm.model = st.selected
            return llm
        except Exception as exc:
            notes.append(f"Local LLM unavailable ({type(exc).__name__}); continued with rules only.")
            return None


def process_document(data: bytes, filename: str, use_llm: bool = False,
                     progress: Optional[ProgressCallback] = None) -> ProcessingResult:
    """Functional entry point used by the UI, scripts and tests."""
    return AgentOrchestrator(use_llm=use_llm, progress=progress).run(data, filename)


__all__ = ["AgentOrchestrator", "ProgressEvent", "STAGES", "process_document", "DocumentRejected"]
