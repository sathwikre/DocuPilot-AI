"""Digital extraction: wide tables, wrapped cells, empty cells, negatives, borderless tables."""
from scripts.evaluate import evaluate_result


def test_invoice_wide_table_exact(invoice_result, invoice):
    _, exp = invoice
    t = invoice_result.tables[0]
    assert t.ncols == 12
    assert t.rows == exp["tables"][0]["rows"]


def test_multiline_cells_are_joined_not_split_into_rows(invoice_result):
    t = invoice_result.tables[0]
    descs = [r[2] for r in t.rows]
    assert "Industrial fastener kit, assorted sizes (240 pc)" in descs
    assert len(t.rows) == 14


def test_negative_numbers_typed_correctly(invoice_result):
    t = invoice_result.tables[0]
    last = t.values[-1]
    qi, ti = t.role_index("quantity"), t.role_index("line_total")
    assert last[qi] == -1
    assert float(last[ti]) == -138.05


def test_report_tables_and_empty_cells(report_result, report_doc):
    _, exp = report_doc
    ev = evaluate_result(report_result, exp)
    assert ev["cell_accuracy"] == 1.0
    assert ev["row_completeness"] == 1.0
    t1 = report_result.tables[0]
    new_markets = next(r for r in t1.rows if r[0] == "New Markets")
    assert new_markets[1:4] == ["", "", ""], "legitimately empty cells must be preserved"
    islands = next(i for i, r in enumerate(t1.rows) if r[0] == "Islands")
    assert float(t1.values[islands][2]) < 0, "(9.0) must be read as a negative number"


def test_wide_14_column_table(report_result):
    t1 = report_result.tables[0]
    assert t1.ncols == 14 and t1.original_header[-1] == "FY Total"


def test_borderless_table_detected(report_result):
    t2 = report_result.tables[1]
    assert "pdf-layout" in t2.methods
    assert t2.ncols == 10 and len(t2.rows) == 10
    assert t2.title.startswith("Table 2: Inventory Snapshot")
