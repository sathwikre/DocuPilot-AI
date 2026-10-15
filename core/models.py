"""Shared data structures passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

BBox = tuple[float, float, float, float]


class DocuPilotError(Exception):
    """Base class for user-facing processing errors."""


class DocumentRejected(DocuPilotError):
    """The document cannot be processed at all (encrypted, malformed, too many pages...)."""


@dataclass
class Word:
    """A word with its bounding box in PDF points (top-left origin)."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: Optional[float] = None

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 0.1)

    @property
    def w(self) -> float:
        return max(self.x1 - self.x0, 0.1)


@dataclass
class PageInfo:
    """Inspection result for a single page."""

    number: int
    width: float
    height: float
    rotation: int
    char_count: int
    image_coverage: float
    line_count: int
    garbled_ratio: float
    kind: str  # digital | scanned | mixed | empty
    likely_table: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class DocumentInfo:
    """Inspection result for a whole document."""

    filename: str
    source_type: str  # pdf | image
    page_count: int
    pages: list[PageInfo]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RawTable:
    """A table fragment extracted from a single page, before cross-page reconstruction."""

    page: int
    bbox: BBox
    header: Optional[list[str]]
    rows: list[list[str]]
    method: str
    page_width: float
    page_height: float
    title: Optional[str] = None
    col_edges: list[float] = field(default_factory=list)  # normalised (0..1) x boundaries
    row_ops: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_widths: list[int] = field(default_factory=list)
    quality: float = 0.0
    ocr: bool = False
    repeated_headers_removed: int = 0
    index_on_page: int = 0
    is_last_on_page: bool = True
    is_first_on_page: bool = True

    @property
    def ncols(self) -> int:
        if self.header:
            return len(self.header)
        return max((len(r) for r in self.rows), default=0)

    @property
    def ref(self) -> str:
        return f"p{self.page}#{self.index_on_page + 1}"


@dataclass
class RowMeta:
    """Provenance and processing notes for one output row."""

    row_id: str
    page: int
    method: str
    kind: str = "data"  # data | summary
    ops: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    segment: str = ""


@dataclass
class MergeDecision:
    """Record of a decision whether to stitch two table fragments together."""

    left: str
    right: str
    merged: bool
    score: float
    evidence: list[str]
    uncertainty: Optional[str] = None
    table_id: Optional[str] = None


@dataclass
class LogicalTable:
    """A reconstructed (possibly multi-page) table with normalised schema and typed values."""

    table_id: str
    title: Optional[str]
    pages: list[int]
    original_header: list[str]
    columns: list[str]
    roles: list[Optional[str]]
    rows: list[list[str]]
    row_meta: list[RowMeta]
    methods: list[str]
    header_ops: list[str] = field(default_factory=list)
    merge_decisions: list[MergeDecision] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    repeated_headers_removed: list[dict[str, Any]] = field(default_factory=list)
    col_types: list[str] = field(default_factory=list)
    values: list[list[Any]] = field(default_factory=list)
    cell_notes: dict[tuple[int, int], list[str]] = field(default_factory=dict)
    header_generated: bool = False

    @property
    def ncols(self) -> int:
        return len(self.columns)

    def role_index(self, role: str) -> Optional[int]:
        """Index of the first column with the given semantic role, if any."""
        for i, r in enumerate(self.roles):
            if r == role:
                return i
        return None

    def data_row_indices(self) -> list[int]:
        return [i for i, m in enumerate(self.row_meta) if m.kind == "data"]


@dataclass
class DocField:
    """A key/value field found outside (or as a summary row of) a table, e.g. 'Grand Total'."""

    key: str
    label: str
    raw: str
    value: Any
    page: int
    source: str  # text | table-row
    table_id: Optional[str] = None


@dataclass
class ValidationIssue:
    """One validation finding. status is Passed, Failed or Unable to Verify."""

    check: str
    severity: str  # critical | warning | info | passed
    status: str
    explanation: str
    suggested_action: str = ""
    table_id: Optional[str] = None
    row_id: Optional[str] = None
    page: Optional[int] = None
    column: Optional[str] = None
    extracted: Any = None
    expected: Any = None
    difference: Any = None
    confirmed: bool = False
    row_index: Optional[int] = None
    col_index: Optional[int] = None


@dataclass
class RemovedRow:
    """A row taken out by a clean-up option. Listed on the workbook's 'Removed rows' sheet."""

    table_id: str
    row_id: str
    page: int
    cells: list[str]
    reason: str


@dataclass
class PageReport:
    """What happened to each page during processing."""

    page: int
    kind: str
    method: str = ""
    ocr_used: bool = False
    tables_found: int = 0
    status: str = "pending"  # ok | no_tables | warning | failed
    warnings: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    ocr_confidence: Optional[float] = None


@dataclass
class ProcessingResult:
    """Everything produced by one run of the pipeline."""

    file_name: str
    file_hash: str
    doc_info: DocumentInfo
    doc_type: str
    page_reports: list[PageReport]
    tables: list[LogicalTable]
    issues: list[ValidationIssue]
    fields: list[DocField]
    merge_decisions: list[MergeDecision]
    stage_timings: dict[str, float]
    elapsed: float
    warnings: list[str]
    excel_bytes: bytes = b""
    llm_used: bool = False
    llm_model: Optional[str] = None
    llm_notes: list[str] = field(default_factory=list)
    # Clean-up checklist state (core.cleanup). base_tables are the reconstructed tables before any clean-up or
    # typing, so the workbook can be rebuilt with other options without reading the document again.
    options: frozenset[str] = frozenset()
    checklist: dict[str, dict] = field(default_factory=dict)
    removed_rows: list[RemovedRow] = field(default_factory=list)
    base_tables: list[LogicalTable] = field(default_factory=list, repr=False)
    line_fields: list[DocField] = field(default_factory=list, repr=False)
    ocr_mode: str = "auto"
    pages_total: Optional[int] = None  # pages in the file when only the first max_pages were converted
    rebuild_seconds: Optional[float] = None

    @property
    def ocr_pages(self) -> list[int]:
        return [p.page for p in self.page_reports if p.ocr_used]

    @property
    def failed_pages(self) -> list[int]:
        return [p.page for p in self.page_reports if p.status == "failed"]

    def metrics(self) -> dict[str, Any]:
        """Real, computed metrics for dashboards and history."""
        sev = {"critical": 0, "warning": 0, "info": 0, "passed": 0}
        for i in self.issues:
            sev[i.severity] = sev.get(i.severity, 0) + 1
        return {
            "pages": self.doc_info.page_count,
            "tables": len(self.tables),
            "rows": sum(len(t.data_row_indices()) for t in self.tables),
            "columns": sum(t.ncols for t in self.tables),
            "max_columns": max((t.ncols for t in self.tables), default=0),
            "critical": sev["critical"],
            "warnings": sev["warning"],
            "info": sev["info"],
            "passed": sev["passed"],
            "ocr_pages": len(self.ocr_pages),
            "failed_pages": len(self.failed_pages),
            "elapsed": round(self.elapsed, 2),
        }
