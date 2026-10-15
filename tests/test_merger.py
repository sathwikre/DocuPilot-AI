"""Multi-page stitching: repeated headers, continued rows, unrelated tables, duplicate safety."""
from core.models import RawTable
from core.table_merger import build_logical_tables

H = ["Date", "Description", "Debit", "Credit", "Balance"]


def frag(page, header, rows, title=None, top=60.0, bottom=780.0, edges=None, first=True, last=True, idx=0):
    return RawTable(page=page, bbox=(40, top, 560, bottom), header=header, rows=rows, method="test",
                    page_width=595, page_height=842, title=title, col_edges=edges or [0.07, 0.2, 0.55, 0.7, 0.82, 0.94],
                    row_ops=[[] for _ in rows], is_first_on_page=first, is_last_on_page=last, index_on_page=idx)


ROWS1 = [["01/03/2024", "Coffee", "4.50", "", "995.50"], ["02/03/2024", "Salary", "", "1,000.00", "1,995.50"]]
ROWS2 = [["03/03/2024", "Rent", "500.00", "", "1,495.50"]]


def test_repeated_header_merges_and_is_removed():
    tables, decisions = build_logical_tables([frag(1, H, ROWS1), frag(2, H, ROWS2, top=50)])
    assert len(tables) == 1
    assert len(tables[0].rows) == 3
    assert decisions[0].merged and any("header repeated" in e for e in decisions[0].evidence)
    assert tables[0].repeated_headers_removed[0]["page"] == 2


def test_headerless_continuation_merges_on_type_evidence():
    tables, decisions = build_logical_tables([frag(1, H, ROWS1), frag(2, None, ROWS2, top=50)])
    assert len(tables) == 1 and decisions[0].merged


def test_continued_row_at_page_boundary_is_joined():
    rows1 = ROWS1 + [["04/03/2024", "NEFT/ACME LOGISTICS", "94.26", "", "1,901.24"]]
    rows2 = [["", "FREIGHT INV 4471", "", "", ""]] + ROWS2
    tables, _ = build_logical_tables([frag(1, H, rows1), frag(2, H, rows2, top=50)])
    t = tables[0]
    assert len(t.rows) == 4
    assert t.rows[2][1] == "NEFT/ACME LOGISTICS FREIGHT INV 4471"
    assert any("joined continued text" in op for op in t.row_meta[2].ops)


def test_same_column_count_but_different_header_is_not_merged():
    other = ["SKU", "Item", "Qty", "Cost", "Value"]
    tables, decisions = build_logical_tables([
        frag(1, H, ROWS1), frag(2, other, [["A-1", "Bolt", "4", "1.00", "4.00"]], top=50)])
    assert len(tables) == 2
    assert not decisions[0].merged
    assert any("different header" in e for e in decisions[0].evidence)


def test_new_title_blocks_headerless_merge():
    tables, d = build_logical_tables([frag(1, H, ROWS1, title="Table 1: Transactions"),
                                      frag(2, None, ROWS2, title="Table 2: Fees", top=50)])
    assert len(tables) == 2 and not d[0].merged


def test_column_count_mismatch_blocks_merge():
    tables, d = build_logical_tables([frag(1, H, ROWS1), frag(2, H[:4], [r[:4] for r in ROWS2], top=50)])
    assert len(tables) == 2 and any("column count differs" in e for e in d[0].evidence)


def test_non_consecutive_pages_never_merge():
    tables, _ = build_logical_tables([frag(1, H, ROWS1), frag(3, H, ROWS2, top=50)])
    assert len(tables) == 2


def test_table_followed_by_other_table_on_same_page_does_not_merge_across_page():
    a = frag(1, H, ROWS1, last=False)
    b = frag(1, ["X", "Y", "Z", "W", "V"], [["a", "b", "1", "2", "3"]], first=False, idx=1, top=500)
    c = frag(2, H, ROWS2, top=50)
    tables, _ = build_logical_tables([a, b, c])
    assert len(tables) == 3


def test_identical_row_at_boundary_with_balance_is_removed_as_extraction_duplicate():
    tables, _ = build_logical_tables([frag(1, H, ROWS1), frag(2, H, [ROWS1[-1]] + ROWS2, top=50)])
    t = tables[0]
    assert len(t.rows) == 3
    assert any("same transaction printed twice" in w for w in t.warnings)


def test_identical_row_at_boundary_without_proof_is_kept_and_flagged():
    h = ["Date", "Description", "Qty", "Price", "Amount"]
    r1 = [["01/03", "Bolt", "2", "1.00", "2.00"]]
    tables, _ = build_logical_tables([frag(1, h, r1), frag(2, h, r1, top=50)])
    t = tables[0]
    assert len(t.rows) == 2, "legitimate repeated records must not be deleted"
    assert any("kept" in op for op in t.row_meta[1].ops)


def test_scanned_statement_merges_three_pages(statement_result):
    assert len(statement_result.tables) == 1
    t = statement_result.tables[0]
    assert t.pages == [1, 2, 3]
    assert len(t.data_row_indices()) == 46
    assert all(d.merged for d in statement_result.merge_decisions)
    joined = [r for r in t.rows if "FREIGHT INV 4471" in r[2]]
    assert joined and joined[0][2] == "NEFT/ACME LOGISTICS PVT LTD FREIGHT INV 4471"


def test_report_unrelated_tables_stay_separate(report_result):
    assert [len(t.pages) for t in report_result.tables] == [2, 1, 1]
    rejected = [d for d in report_result.merge_decisions if not d.merged]
    assert rejected and rejected[0].left == "p2#2" and rejected[0].right == "p3#1"
