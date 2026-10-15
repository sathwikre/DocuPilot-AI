"""Validation engine: deliberate discrepancies, duplicates, shifts, missing values, no silent edits."""
import copy
from decimal import Decimal

from core.data_normalizer import type_table
from core.models import DocField, LogicalTable, RowMeta
from core.validator import Validator


def make_table(header, rows, method="pdf-ruled"):
    t = LogicalTable(table_id="T1", title=None, pages=[1], original_header=header, columns=list(header),
                     roles=[None] * len(header), rows=[list(r) for r in rows],
                     row_meta=[RowMeta(row_id=f"T1-R{i + 1}", page=1, method=method) for i in range(len(rows))],
                     methods=[method])
    type_table(t)
    return t


def crit(issues, check=None):
    return [i for i in issues if i.severity == "critical" and (check is None or i.check == check)]


def test_invoice_planted_errors_detected(invoice_result):
    c = crit(invoice_result.issues)
    assert {i.check for i in c} == {"line_arithmetic", "line_total_sum"}
    line = next(i for i in c if i.check == "line_arithmetic")
    assert line.row_id == "T1-R7" and line.column == "Line Total"
    assert abs(line.difference - 10.0) < 1e-9 and line.confirmed


def test_invoice_totals_pass(invoice_result):
    passed = {i.check for i in invoice_result.issues if i.severity == "passed"}
    assert {"subtotal", "tax_total", "grand_total"} <= passed


def test_validation_never_modifies_values(invoice):
    from core.agent_orchestrator import process_document
    r = process_document(invoice[0], "inv.pdf")
    t = r.tables[0]
    before = copy.deepcopy(t.values)
    Validator().validate(r.tables, r.fields, r.doc_type)
    assert t.values == before
    assert t.rows[6][-1] == "334.87", "the wrong line total stays exactly as printed"


def test_report_row_total_error_detected(report_result):
    c = crit(report_result.issues, "row_total")
    assert len(c) == 1 and c[0].row_id == "T1-R4" and abs(c[0].difference - 10.0) < 1e-9


def test_statement_reconciles_and_keeps_lookalike_duplicates(statement_result):
    issues = statement_result.issues
    assert not crit(issues)
    assert any(i.check == "running_balance" and i.severity == "passed" for i in issues)
    assert any(i.check == "statement_reconciliation" and i.status == "Passed" for i in issues)
    dup = [i for i in issues if i.check == "duplicate_rows"]
    assert dup and dup[0].severity == "info" and "kept" in dup[0].explanation


STMT_H = ["Date", "Description", "Debit", "Credit", "Balance"]
STMT = [["01/03/2024", "Coffee", "4.50", "", "995.50"],
        ["02/03/2024", "Salary", "", "1,000.00", "1,995.50"],
        ["03/03/2024", "Rent", "500.00", "", "1,495.50"],
        ["04/03/2024", "Fuel", "45.00", "", "1,450.50"],
        ["05/03/2024", "Refund", "", "20.00", "1,470.50"],
        ["06/03/2024", "Books", "30.00", "", "1,440.50"],
        ["07/03/2024", "Gym", "40.00", "", "1,400.50"],
        ["08/03/2024", "Bonus", "", "100.00", "1,500.50"]]


def test_wrong_running_balance_detected():
    rows = copy.deepcopy(STMT)
    rows[2][4] = "1,485.50"   # misread balance
    t = make_table(STMT_H, rows)
    issues = Validator().validate([t], [DocField("opening_balance", "Opening", "1000", Decimal("1000"), 1, "text")],
                                  "bank_statement")
    rb = crit(issues, "running_balance")
    assert [i.row_id for i in rb] == ["T1-R3", "T1-R4"]
    assert "probably misread" in rb[0].explanation


def test_unable_to_verify_without_opening_or_convention():
    t = make_table(STMT_H, [["01/03/2024", "A", "1.00", "", "5.00"], ["02/03/2024", "B", "7.00", "", "99.00"]])
    issues = Validator().validate([t], [], "bank_statement")
    rb = [i for i in issues if i.check == "running_balance"]
    assert rb and all(i.status != "Passed" for i in rb), "must not claim a pass without enough information"


def test_missing_invoice_totals_are_unable_to_verify(invoice_result):
    t = invoice_result.tables[0]
    issues = Validator().validate([t], [], "invoice")
    gt = [i for i in issues if i.check == "grand_total"]
    assert gt and gt[0].status == "Unable to Verify"


def test_exact_duplicate_flagged_but_not_removed():
    h = ["SKU", "Item", "Qty", "Price"]
    rows = [["A1", "Bolt", "4", "1.00"], ["A2", "Nut", "5", "0.50"], ["A1", "Bolt", "4", "1.00"]]
    t = make_table(h, rows)
    issues = Validator().validate([t], [], "generic")
    d = [i for i in issues if i.check == "duplicate_rows"]
    assert d and d[0].severity == "warning" and not d[0].confirmed
    assert len(t.rows) == 3


def test_column_shift_detected():
    h = ["Date", "Description", "Amount", "Balance", "Ref"]
    rows = [["01/03/2024", "Coffee shop", "4.50", "100.00", "AB1"],
            ["02/03/2024", "Fuel station", "40.00", "60.00", "AB2"],
            ["03/03/2024", "Book store", "12.00", "48.00", "AB3"],
            ["04/03/2024", "Grocery", "30.00", "18.00", "AB4"],
            ["05/03/2024", "5.00", "13.00", "AB5", ""]]   # cells shifted one column left
    t = make_table(h, rows)
    issues = Validator().validate([t], [], "generic")
    assert any(i.check == "column_shift" and i.row_id == "T1-R5" for i in issues)


def test_sequence_gap_suggests_missing_rows():
    h = ["#", "Item", "Qty", "Unit Price", "Amount"]
    rows = [["1", "A", "1", "2.00", "2.00"], ["2", "B", "1", "3.00", "3.00"], ["5", "C", "2", "1.00", "2.00"]]
    t = make_table(h, rows)
    issues = Validator().validate([t], [], "invoice")
    gap = [i for i in issues if i.check == "sequence_gap"]
    assert gap and gap[0].difference == 2


def test_inconsistent_type_flagged():
    h = ["Date", "Item", "Amount"]
    rows = [["01/03/2024", "a", "1.00"], ["02/03/2024", "b", "2.00"], ["03/03/2024", "c", "3.00"],
            ["04/03/2024", "d", "4.00"], ["05/03/2024", "e", "N/A"]]
    t = make_table(h, rows)
    issues = Validator().validate([t], [], "generic")
    assert any(i.check == "data_type" and i.row_id == "T1-R5" for i in issues)
    assert t.values[4][2] == "N/A", "unparseable value kept as original text"


def test_ocr_digit_repair_is_audited():
    h = ["Date", "Item", "Amount"]
    rows = [["01/03/2024", "a", "1.00"], ["02/03/2024", "b", "2.00"], ["03/03/2024", "c", "1O0.00"]]
    t = make_table(h, rows, method="ocr-layout")
    assert t.values[2][2] == Decimal("100.00")
    assert any("OCR repair" in op for op in t.row_meta[2].ops)


def test_every_issue_has_valid_status(invoice_result, report_result):
    for r in (invoice_result, report_result):
        for i in r.issues:
            assert i.status in ("Passed", "Failed", "Unable to Verify")
            assert i.severity in ("critical", "warning", "info", "passed")
            if i.severity == "passed":
                assert i.status == "Passed"


def test_unreadable_amounts_make_reconciliation_unverifiable_not_failed():
    rows = copy.deepcopy(STMT)
    rows[1][3] = "1.0OO.0O,"  # garbled credit that can be neither parsed nor safely repaired
    t = make_table(STMT_H, rows)
    fields = [DocField("opening_balance", "Opening", "1000", Decimal("1000"), 1, "text"),
              DocField("closing_balance", "Closing", "1500.50", Decimal("1500.50"), 1, "text")]
    issues = Validator().validate([t], fields, "bank_statement")
    assert not crit(issues), "unreadable cells must not produce false critical findings"
    rec = [i for i in issues if i.check == "statement_reconciliation"]
    assert rec and rec[0].status == "Unable to Verify"
    assert t.role_index("credit") is not None, "a few garbled cells must not drop the column's role"


def test_ocr_thousands_separator_repair():
    from core.value_parser import ocr_repair_number
    parsed, txt = ocr_repair_number("10.695.60")
    assert txt == "10,695.60" and parsed.value == Decimal("10695.60")
