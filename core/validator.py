"""Step E: rule-based validation. All arithmetic is deterministic Decimal arithmetic.

Every check yields Passed, Failed or Unable to Verify. Nothing is ever corrected here: discrepancies
are reported with the extracted value, the expected value and the difference, and the source data is
left untouched.

Severity: critical  = confirmed numerical failure under an established convention
          warning   = suspected problem that needs review (not proven)
          info      = informational / unable to verify
          passed    = check performed and satisfied
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from core.models import DocField, LogicalTable, PageReport, ValidationIssue

D = Decimal
ZERO = D(0)

PASSED, FAILED, UNVERIFIED = "Passed", "Failed", "Unable to Verify"


def _num(v: Any) -> Optional[Decimal]:
    return v if isinstance(v, Decimal) else None


def _fmt(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


def _q(v: Decimal) -> Decimal:
    return v.quantize(D("0.01"))


class Validator:
    """Runs all applicable checks for a document."""

    def __init__(self, money_tolerance: str = "0.011"):
        self.tol = D(money_tolerance)
        self.issues: list[ValidationIssue] = []

    # ------------------------------------------------------------------ helpers
    def _add(self, check: str, severity: str, status: str, explanation: str, action: str = "",
             table: Optional[LogicalTable] = None, row: Optional[int] = None, col: Optional[int] = None,
             extracted: Any = None, expected: Any = None, difference: Any = None, confirmed: bool = False,
             page: Optional[int] = None) -> None:
        row_id = table.row_meta[row].row_id if (table is not None and row is not None) else None
        if page is None and table is not None and row is not None:
            page = table.row_meta[row].page
        self.issues.append(ValidationIssue(
            check=check, severity=severity, status=status, explanation=explanation, suggested_action=action,
            table_id=table.table_id if table is not None else None, row_id=row_id, page=page,
            column=table.columns[col] if (table is not None and col is not None) else None,
            extracted=_fmt(extracted), expected=_fmt(expected),
            difference=_fmt(difference), confirmed=confirmed, row_index=row, col_index=col,
        ))

    def _passed(self, check: str, table: Optional[LogicalTable], n_ok: int, n_total: int, what: str) -> None:
        if n_ok:
            self._add(check, "passed", PASSED, f"{what}: {n_ok} of {n_total} verified.", table=table,
                      page=(table.pages[0] if table is not None and table.pages else None))

    def _close(self, a: Decimal, b: Decimal, tol: Optional[Decimal] = None) -> bool:
        return abs(a - b) <= (tol if tol is not None else self.tol)

    # ------------------------------------------------------------------ entry point
    def validate(self, tables: Sequence[LogicalTable], fields: Sequence[DocField], doc_type: str,
                 page_reports: Sequence[PageReport] = ()) -> list[ValidationIssue]:
        self.issues = []
        self._document_checks(tables, page_reports)
        invoice_done = statement_done = False
        for t in tables:
            self._structure_checks(t)
            balanced_rows: set[int] = set()
            total_ok_rows: set[int] = set()
            if not invoice_done and t.role_index("quantity") is not None and t.role_index("unit_price") is not None:
                self._invoice_lines(t)
                self._invoice_totals(t, [f for f in fields if f.table_id in (None, t.table_id)])
                invoice_done = True
            elif t.role_index("balance") is not None and (
                    t.role_index("debit") is not None or t.role_index("credit") is not None or t.role_index("amount") is not None):
                balanced_rows = self._statement(t, [f for f in fields if f.table_id in (None, t.table_id)] if not statement_done else [])
                statement_done = True
            else:
                total_ok_rows = self._row_totals(t)
            self._column_totals(t)
            self._type_checks(t)
            self._missing_values(t, total_ok_rows)
            self._column_shift(t)
            self._duplicates(t, balanced_rows)
            self._sequence(t)
            self._date_order(t)
        return self.issues

    # ------------------------------------------------------------------ document level
    def _document_checks(self, tables: Sequence[LogicalTable], reports: Sequence[PageReport]) -> None:
        for r in reports:
            if r.status == "failed":
                self._add("page_processing", "critical", FAILED,
                          f"Page {r.page} could not be processed: {'; '.join(r.warnings) or 'unknown error'}.",
                          "Check the page in the source document and enter its data manually if needed.", page=r.page)
            if r.ocr_used and r.ocr_confidence is not None and r.ocr_confidence < 0.85:
                self._add("ocr_confidence", "warning", FAILED,
                          f"OCR confidence on page {r.page} is low ({r.ocr_confidence:.0%}). Values may be misread.",
                          "Spot-check numbers on this page against the source.", page=r.page)
        if not tables:
            self._add("tables_found", "info", UNVERIFIED, "No tables were detected in the document, so no data checks could run.",
                      "Confirm the document contains tables; scanned pages need legible text.")

    def _structure_checks(self, t: LogicalTable) -> None:
        if t.header_generated:
            self._add("header_detection", "warning", FAILED,
                      "No header row was recognised; generic column names were assigned.",
                      "Rename columns after export, or check whether the header was cut off.", table=t, page=t.pages[0])
        for rh in t.repeated_headers_removed:
            self._add("repeated_header", "info", PASSED,
                      f"Repeated header row removed on page {rh['page']} ({rh['where']}). No data rows were removed.",
                      table=t, page=rh["page"])
        for d in t.merge_decisions:
            if d.merged and d.uncertainty:
                self._add("table_merge", "warning", FAILED,
                          f"Fragments {d.left} and {d.right} were merged with {d.uncertainty}. Evidence: {'; '.join(d.evidence)}.",
                          "Review rows at the page boundary.", table=t)
            elif not d.merged and d.uncertainty:
                self._add("table_merge", "info", UNVERIFIED,
                          f"Fragments {d.left} and {d.right} were kept separate: {d.uncertainty}. Evidence: {'; '.join(d.evidence)}.",
                          "If they are one table, combine them after export.", table=t)
        for ri, m in enumerate(t.row_meta):
            for op in m.ops:
                if op.startswith("row width"):
                    self._add("row_width", "warning", FAILED, f"Row had an unexpected number of cells ({op}).",
                              "Check this row for merged or split cells.", table=t, row=ri)
                if op.startswith("identical to previous row across a page boundary"):
                    self._add("boundary_duplicate", "warning", FAILED,
                              "This row is identical to the last row of the previous page. It was kept because there is no "
                              "proof that it is an extraction duplicate.", "Confirm against the source whether it appears twice.",
                              table=t, row=ri)

    # ------------------------------------------------------------------ invoices
    def _invoice_lines(self, t: LogicalTable) -> None:
        qi, pi = t.role_index("quantity"), t.role_index("unit_price")
        ni, ti = t.role_index("net_amount"), t.role_index("line_total")
        ri_, ai = t.role_index("tax_rate"), t.role_index("tax_amount")
        dri, dai = t.role_index("discount_rate"), t.role_index("discount_amount")
        rows = t.data_row_indices()
        net_ok = tax_ok = tot_ok = 0
        n_net = n_tax = 0
        # Establish what 'Line Total' includes by testing conventions across all rows.
        conv_votes = Counter()
        computed: dict[int, dict[str, Decimal]] = {}
        for r in rows:
            v = t.values[r]
            q, p = _num(v[qi]), _num(v[pi])
            if q is None or p is None:
                self._add("line_arithmetic", "info", UNVERIFIED, "Quantity or unit price is missing or not numeric, so the line cannot be recomputed.",
                          "Check the quantity and price cells.", table=t, row=r)
                continue
            gross = q * p
            disc_rate = _num(v[dri]) if dri is not None else None
            disc_amt = _num(v[dai]) if dai is not None else None
            net_exp = gross * (1 - (disc_rate or ZERO)) - (disc_amt or ZERO)
            c = {"net": net_exp}
            net_ext = _num(v[ni]) if ni is not None else None
            base = net_ext if net_ext is not None else net_exp
            if ri_ is not None and _num(v[ri_]) is not None:
                c["tax"] = base * _num(v[ri_])
            tax_ext = _num(v[ai]) if ai is not None else None
            tax_for_total = tax_ext if tax_ext is not None else c.get("tax")
            if ti is not None and _num(v[ti]) is not None:
                tot = _num(v[ti])
                if tax_for_total is not None and self._close(_q(base + tax_for_total), tot, self.tol * 2):
                    conv_votes["net+tax"] += 1
                if self._close(_q(base), tot, self.tol * 2):
                    conv_votes["net"] += 1
                if self._close(_q(gross), tot, self.tol * 2):
                    conv_votes["gross"] += 1
            c["base"], c["tax_for_total"], c["gross"] = base, tax_for_total if tax_for_total is not None else ZERO, gross
            computed[r] = c
        convention = None
        if conv_votes:
            convention, votes = conv_votes.most_common(1)[0]
            if votes < max(2, 0.6 * len(computed)):
                convention = None
        for r, c in computed.items():
            v = t.values[r]
            disc_txt = " − discount" if (dri is not None or dai is not None) else ""
            if ni is not None and _num(v[ni]) is not None:
                n_net += 1
                exp = _q(c["net"])
                if self._close(exp, _num(v[ni])):
                    net_ok += 1
                else:
                    self._add("line_arithmetic", "critical", FAILED,
                              f"Quantity × Unit Price{disc_txt} = {exp:,.2f}, but the Net Amount shows {_num(v[ni]):,.2f}.",
                              "Compare this line with the source; the printed amount or the quantity/price may be wrong.",
                              table=t, row=r, col=ni, extracted=_num(v[ni]), expected=exp, difference=_num(v[ni]) - exp,
                              confirmed=True)
            if "tax" in c and ai is not None and _num(v[ai]) is not None:
                n_tax += 1
                exp = _q(c["tax"])
                if self._close(exp, _num(v[ai])):
                    tax_ok += 1
                else:
                    self._add("line_arithmetic", "critical", FAILED,
                              f"Net × Tax rate = {exp:,.2f}, but the Tax Amount shows {_num(v[ai]):,.2f}.",
                              "Check the tax rate and tax amount for this line.", table=t, row=r, col=ai,
                              extracted=_num(v[ai]), expected=exp, difference=_num(v[ai]) - exp, confirmed=True)
            if ti is not None and _num(v[ti]) is not None:
                tot = _num(v[ti])
                if convention is None:
                    continue
                exp = _q({"net+tax": c["base"] + c["tax_for_total"], "net": c["base"], "gross": c["gross"]}[convention])
                if self._close(exp, tot, self.tol * 2):
                    tot_ok += 1
                else:
                    formula = {"net+tax": "Net + Tax", "net": "Net amount", "gross": "Quantity × Unit Price"}[convention]
                    self._add("line_arithmetic", "critical", FAILED,
                              f"{formula} = {exp:,.2f}, but '{t.columns[ti]}' shows {tot:,.2f} "
                              f"(convention '{convention}' holds on {conv_votes[convention]} of {len(computed)} lines).",
                              "Verify the printed line total; do not adjust totals without checking the source.",
                              table=t, row=r, col=ti, extracted=tot, expected=exp, difference=tot - exp, confirmed=True)
        if ti is not None and convention is None and computed:
            self._add("line_arithmetic", "info", UNVERIFIED,
                      f"Could not establish how '{t.columns[ti]}' is computed (tested net+tax, net, qty×price); line totals not verified.",
                      "Check the invoice's pricing rules manually.", table=t, page=t.pages[0])
        self._passed("line_arithmetic", t, net_ok, n_net, "Net amount = quantity × price − discount")
        self._passed("line_arithmetic", t, tax_ok, n_tax, "Tax amount = net × rate")
        if convention:
            self._passed("line_arithmetic", t, tot_ok, sum(1 for r in computed if _num(t.values[r][ti]) is not None),
                         f"Line totals ({convention})")

    @staticmethod
    def _unreadable(t: LogicalTable, ci: Optional[int]) -> int:
        """Data cells in column ci that hold text but no parsed number (e.g. OCR-garbled amounts)."""
        if ci is None:
            return 0
        return sum(1 for r in t.data_row_indices() if t.rows[r][ci] and _num(t.values[r][ci]) is None)

    def _col_sum(self, t: LogicalTable, ci: Optional[int]) -> Optional[Decimal]:
        """Column sum over data rows; None when absent or when any cell is unreadable (the sum would be wrong)."""
        if ci is None or self._unreadable(t, ci):
            return None
        vals = [_num(t.values[r][ci]) for r in t.data_row_indices()]
        nums = [v for v in vals if v is not None]
        return sum(nums, ZERO) if nums else None

    def _invoice_totals(self, t: LogicalTable, fields: Sequence[DocField]) -> None:
        f = {}
        for fld in fields:
            f.setdefault(fld.key, fld)
        sum_net = self._col_sum(t, t.role_index("net_amount"))
        sum_tax = self._col_sum(t, t.role_index("tax_amount"))
        sum_tot = self._col_sum(t, t.role_index("line_total"))

        unreadable = {t.columns[c]: self._unreadable(t, c) for c in (t.role_index("net_amount"), t.role_index("tax_amount"),
                                                                    t.role_index("line_total")) if c is not None}
        unreadable = {k: v for k, v in unreadable.items() if v}

        def check(name: str, label: str, expected: Optional[Decimal], fld: Optional[DocField], basis: str):
            if fld is not None and expected is None and unreadable:
                self._add(name, "info", UNVERIFIED,
                          f"{label} check skipped: " + ", ".join(f"{n} value(s) in '{c}'" for c, n in unreadable.items())
                          + " could not be read as numbers, so the column sum would be unreliable.",
                          "Fix the flagged cells, then re-check the totals.", table=t, page=fld.page)
                return
            if fld is None or expected is None:
                missing = label if fld is None else basis
                self._add(name, "info", UNVERIFIED, f"{label} check skipped: {missing} not found in the document.",
                          "If the document shows this value, verify it manually.", table=t, page=t.pages[-1])
                return
            diff = fld.value - expected
            if self._close(fld.value, expected):
                self._add(name, "passed", PASSED, f"{label} {fld.value:,.2f} equals {basis} ({expected:,.2f}).",
                          table=t, page=fld.page, extracted=fld.value, expected=expected, difference=ZERO)
            else:
                self._add(name, "critical", FAILED,
                          f"{label} shows {fld.value:,.2f} but {basis} is {expected:,.2f}.",
                          "Reconcile the totals with the line items; the document itself may contain the error.",
                          table=t, page=fld.page, extracted=fld.value, expected=expected, difference=diff, confirmed=True)

        sub_basis = sum_net if sum_net is not None else (sum_tot if sum_tax is None else None)
        check("subtotal", "Subtotal", sub_basis, f.get("subtotal"),
              "the sum of line net amounts" if sum_net is not None else "the sum of line totals")
        check("tax_total", "Tax total", sum_tax, f.get("tax_total"), "the sum of line tax amounts")
        if f.get("subtotal") is not None:
            exp_grand = f["subtotal"].value
            parts = ["subtotal"]
            if f.get("tax_total"):
                exp_grand += f["tax_total"].value
                parts.append("tax")
            if f.get("shipping"):
                exp_grand += f["shipping"].value
                parts.append("shipping")
            if f.get("discount_total"):
                exp_grand -= abs(f["discount_total"].value)
                parts.append("− discount")
            check("grand_total", "Grand total", exp_grand, f.get("grand_total"), " + ".join(parts))
        else:
            check("grand_total", "Grand total", None, f.get("grand_total"), "subtotal")
        # Do the line totals add up to the totals block?
        if sum_tot is not None and f.get("subtotal") is not None and sum_tax is not None:
            target = f["subtotal"].value + (f["tax_total"].value if f.get("tax_total") else ZERO)
            if self._close(sum_tot, target):
                self._add("line_total_sum", "passed", PASSED,
                          f"Sum of line totals ({sum_tot:,.2f}) equals subtotal + tax ({target:,.2f}).", table=t,
                          page=t.pages[-1], extracted=sum_tot, expected=target, difference=ZERO)
            else:
                self._add("line_total_sum", "critical", FAILED,
                          f"Sum of line totals is {sum_tot:,.2f}, but subtotal + tax is {target:,.2f}.",
                          "At least one line total is inconsistent with the totals block; see the line arithmetic findings.",
                          table=t, page=t.pages[-1], extracted=sum_tot, expected=target, difference=sum_tot - target,
                          confirmed=True)

    # ------------------------------------------------------------------ statements
    def _statement(self, t: LogicalTable, fields: Sequence[DocField]) -> set[int]:
        bi = t.role_index("balance")
        di, ci, ai = t.role_index("debit"), t.role_index("credit"), t.role_index("amount")
        f = {}
        for fld in fields:
            f.setdefault(fld.key, fld)
        rows = t.data_row_indices()
        opening = f["opening_balance"].value if f.get("opening_balance") else None
        for r, m in enumerate(t.row_meta):
            if m.kind == "summary" and "opening" in " ".join(t.rows[r]).lower() and _num(t.values[r][bi]) is not None:
                opening = opening if opening is not None else _num(t.values[r][bi])

        def delta(r: int, conv: str) -> Optional[Decimal]:
            v = t.values[r]
            if conv == "signed":
                return _num(v[ai]) if ai is not None else None
            dr = _num(v[di]) if di is not None else None
            cr = _num(v[ci]) if ci is not None else None
            raw = t.rows[r]
            if (di is not None and raw[di] and dr is None) or (ci is not None and raw[ci] and cr is None):
                return None  # an amount is present but unreadable: the movement is unknown
            if dr is None and cr is None:
                return None
            dr, cr = abs(dr or ZERO), abs(cr or ZERO)
            return (cr - dr) if conv == "credit_adds" else (dr - cr)

        convs = ["credit_adds", "debit_adds"] + (["signed"] if ai is not None else [])
        votes = Counter()
        tested = 0
        prev = opening
        for r in rows:
            bal = _num(t.values[r][bi])
            if prev is not None and bal is not None:
                tested += 1
                for cv in convs:
                    d = delta(r, cv)
                    if d is not None and self._close(prev + d, bal):
                        votes[cv] += 1
            prev = bal if bal is not None else None
        ok_rows: set[int] = set()
        if not votes or votes.most_common(1)[0][1] < max(2, 0.6 * tested):
            self._add("running_balance", "info", UNVERIFIED,
                      "The debit/credit convention could not be established from the data, so running balances were not verified.",
                      "Check the statement's sign convention manually.", table=t, page=t.pages[0])
            return ok_rows
        conv, nvotes = votes.most_common(1)[0]
        conv_text = {"credit_adds": "previous balance + credit − debit", "debit_adds": "previous balance + debit − credit",
                     "signed": "previous balance + amount"}[conv]
        prev = opening
        prev_row: Optional[int] = None
        fails: list[tuple[int, Decimal]] = []
        n_checked = 0
        for r in rows:
            bal = _num(t.values[r][bi])
            d = delta(r, conv)
            if bal is None:
                self._add("running_balance", "warning", UNVERIFIED, "Balance is missing or not numeric; the running balance chain is broken here.",
                          "Check the balance cell for this row.", table=t, row=r, col=bi)
                prev, prev_row = None, r
                continue
            if prev is None or d is None:
                if prev is None and prev_row is None:
                    self._add("running_balance", "info", UNVERIFIED,
                              "No opening balance was found, so the first row's balance cannot be verified.",
                              "Check the opening balance on the statement.", table=t, row=r, col=bi)
                elif d is None and prev is not None:
                    self._add("running_balance", "info", UNVERIFIED,
                              "This row's debit/credit amount could not be read, so its balance cannot be verified. "
                              "The chain continues from the printed balance.",
                              "Check the amount cells on this row.", table=t, row=r, col=bi)
                prev, prev_row = bal, r
                continue
            n_checked += 1
            exp = prev + d
            if self._close(exp, bal):
                ok_rows.add(r)
                if prev_row is not None:
                    ok_rows.add(prev_row)
            else:
                fails.append((r, bal - exp))
                self._add("running_balance", "critical", FAILED,
                          f"Expected balance {exp:,.2f} ({conv_text}), but the statement shows {bal:,.2f}.",
                          "Check this row's amounts and balance against the source. A difference equal to a transaction "
                          "amount may indicate a missing row.", table=t, row=r, col=bi, extracted=bal, expected=exp,
                          difference=bal - exp, confirmed=True)
            prev, prev_row = bal, r
        # Pattern hint: consecutive failures with opposite differences point at one misread balance.
        for (r1, d1), (r2, d2) in zip(fails, fails[1:]):
            if self._close(d1, -d2) and rows.index(r2) == rows.index(r1) + 1:
                for iss in self.issues:
                    if iss.check == "running_balance" and iss.row_index == r1 and iss.table_id == t.table_id:
                        iss.explanation += " The next row fails by the opposite amount, so this balance figure itself is probably misread."
        self._passed("running_balance", t, n_checked - len(fails), n_checked, f"Running balance ({conv_text})")

        # Opening + credits − debits = closing.
        sum_dr = self._col_sum(t, di) or ZERO
        sum_cr = self._col_sum(t, ci) or ZERO
        sum_amt = self._col_sum(t, ai) or ZERO
        last_bal = next((_num(t.values[r][bi]) for r in reversed(rows) if _num(t.values[r][bi]) is not None), None)
        closing_f = f.get("closing_balance")
        closing = closing_f.value if closing_f else last_bal
        bad_cols = {t.columns[c]: self._unreadable(t, c) for c in (di, ci, ai) if c is not None and self._unreadable(t, c)}
        if bad_cols:
            self._add("statement_reconciliation", "info", UNVERIFIED,
                      "Reconciliation skipped: " + ", ".join(f"{n} value(s) in '{c}'" for c, n in bad_cols.items())
                      + " could not be read as numbers, so the totals would be unreliable.",
                      "Fix the flagged cells and re-check.", table=t, page=t.pages[-1])
        elif opening is not None and closing is not None:
            movement = sum_amt if conv == "signed" else ((sum_cr - sum_dr) if conv == "credit_adds" else (sum_dr - sum_cr))
            exp = opening + movement
            src = "stated closing balance" if closing_f else "last balance in the table"
            if self._close(exp, closing):
                self._add("statement_reconciliation", "passed", PASSED,
                          f"Opening {opening:,.2f} + net movement {movement:,.2f} = {exp:,.2f}, matching the {src}.",
                          table=t, page=t.pages[-1], extracted=closing, expected=exp, difference=ZERO)
            else:
                self._add("statement_reconciliation", "critical", FAILED,
                          f"Opening {opening:,.2f} + net movement {movement:,.2f} = {exp:,.2f}, but the {src} is {closing:,.2f}.",
                          "Look for missing or misread transactions; see running-balance findings.", table=t,
                          page=t.pages[-1], extracted=closing, expected=exp, difference=closing - exp, confirmed=True)
        else:
            self._add("statement_reconciliation", "info", UNVERIFIED,
                      "Opening or closing balance not found; statement reconciliation could not be performed.",
                      "Check the statement summary manually.", table=t, page=t.pages[-1])
        if closing_f and last_bal is not None:
            if self._close(closing_f.value, last_bal):
                self._add("closing_balance", "passed", PASSED, f"Stated closing balance equals the last running balance ({last_bal:,.2f}).",
                          table=t, page=closing_f.page)
            else:
                self._add("closing_balance", "critical", FAILED,
                          f"Stated closing balance {closing_f.value:,.2f} differs from the last running balance {last_bal:,.2f}.",
                          "Check whether rows are missing at the end of the statement.", table=t, page=closing_f.page,
                          extracted=closing_f.value, expected=last_bal, difference=closing_f.value - last_bal, confirmed=True)
        for key, total, label in (("total_debits", sum_dr, "debits"), ("total_credits", sum_cr, "credits")):
            fld = f.get(key)
            if fld is None or (key == "total_debits" and di is None) or (key == "total_credits" and ci is None):
                continue
            col = di if key == "total_debits" else ci
            if self._unreadable(t, col):
                self._add(key, "info", UNVERIFIED,
                          f"Stated total {label} not verified: {self._unreadable(t, col)} value(s) in "
                          f"'{t.columns[col]}' could not be read.", "Fix the flagged cells and re-check.",
                          table=t, page=fld.page)
                continue
            if self._close(abs(fld.value), abs(total)):
                self._add(key, "passed", PASSED, f"Stated total {label} {fld.value:,.2f} equals the column sum.",
                          table=t, page=fld.page, extracted=fld.value, expected=total, difference=ZERO)
            else:
                self._add(key, "critical", FAILED, f"Stated total {label} {fld.value:,.2f} but the column sums to {total:,.2f}.",
                          "Look for missing, duplicated or misread rows.", table=t, page=fld.page,
                          extracted=fld.value, expected=total, difference=abs(fld.value) - abs(total), confirmed=True)
        return ok_rows

    # ------------------------------------------------------------------ generic tables
    def _row_totals(self, t: LogicalTable) -> set[int]:
        """Rows with a 'total' column that should equal the sum of the row's other numeric cells."""
        ti = t.role_index("line_total")
        ok_rows: set[int] = set()
        if ti is None:
            return ok_rows
        sum_cols = [c for c, ty in enumerate(t.col_types) if ty in ("number", "integer") and c != ti
                    and t.roles[c] not in ("line_no", "quantity", "unit_price", "balance", "tax_rate")]
        if len(sum_cols) < 2:
            return ok_rows
        rows = t.data_row_indices()
        results = []
        for r in rows:
            tot = _num(t.values[r][ti])
            parts = [_num(t.values[r][c]) for c in sum_cols]
            if tot is None or all(p is None for p in parts):
                continue
            s = sum((p for p in parts if p is not None), ZERO)
            decimals = max((-p.as_tuple().exponent for p in parts if p is not None), default=2)
            tol = max(self.tol, D(1).scaleb(-decimals) * D("0.51"))
            results.append((r, tot, s, tol, sum(p is not None for p in parts)))
        matches = sum(1 for _r, tot, s, tol, _n in results if abs(tot - s) <= tol)
        if not results or matches < max(2, 0.6 * len(results)):
            if results:
                self._add("row_total", "info", UNVERIFIED,
                          f"'{t.columns[ti]}' does not consistently equal the sum of the other numeric columns, so row totals were not verified.",
                          table=t, page=t.pages[0])
            return ok_rows
        for r, tot, s, tol, n in results:
            diff = tot - s
            rounding = tol * n
            if abs(diff) <= tol:
                ok_rows.add(r)
            elif abs(diff) <= rounding:
                self._add("row_total", "warning", FAILED,
                          f"Row total {tot:,} differs from the sum of its parts ({s:,}) by {diff:,}, which is within possible rounding.",
                          "Likely rounding in the source; confirm if precision matters.", table=t, row=r, col=ti,
                          extracted=tot, expected=s, difference=diff)
            else:
                self._add("row_total", "critical", FAILED,
                          f"'{t.columns[ti]}' shows {tot:,} but the {n} numeric cells in this row sum to {s:,}.",
                          "Check the row's values and its total in the source.", table=t, row=r, col=ti,
                          extracted=tot, expected=s, difference=diff, confirmed=True)
        self._passed("row_total", t, len(ok_rows), len(results), f"Row totals ('{t.columns[ti]}' = sum of row)")
        return ok_rows

    def _column_totals(self, t: LogicalTable) -> None:
        """A 'Total' summary row inside a table should equal the column sums above it."""
        start = 0
        for r, m in enumerate(t.row_meta):
            if m.kind != "summary":
                continue
            label = " ".join(t.rows[r]).lower()
            if "total" not in label:
                start = r + 1
                continue
            block = [i for i in range(start, r) if t.row_meta[i].kind == "data"]
            for c, ty in enumerate(t.col_types):
                v = _num(t.values[r][c])
                if ty not in ("number", "integer") or v is None or t.roles[c] in ("line_no", "unit_price", "tax_rate", "balance"):
                    continue
                s = sum((_num(t.values[i][c]) or ZERO for i in block), ZERO)
                if self._close(v, s):
                    self._add("column_total", "passed", PASSED, f"Total row for '{t.columns[c]}' equals the column sum ({s:,}).",
                              table=t, row=r, col=c)
                else:
                    self._add("column_total", "critical", FAILED,
                              f"Total row shows {v:,} for '{t.columns[c]}', but the {len(block)} rows above sum to {s:,}.",
                              "Check for missing or misread rows above this total.", table=t, row=r, col=c,
                              extracted=v, expected=s, difference=v - s, confirmed=True)
            start = r + 1

    def _type_checks(self, t: LogicalTable) -> None:
        n_cells = 0
        for (r, c), notes in sorted(t.cell_notes.items()):
            n_cells += 1
            self._add("data_type", "warning", FAILED, "; ".join(notes) + f" ('{t.rows[r][c]}').",
                      "Check the source cell; this may be an OCR misread or a value in the wrong column.",
                      table=t, row=r, col=c, extracted=t.rows[r][c])
        typed_cols = sum(1 for ty in t.col_types if ty in ("number", "integer", "percent", "date"))
        if typed_cols and not n_cells:
            self._add("data_type", "passed", PASSED,
                      f"All values in {typed_cols} numeric/date columns parsed as their column type.", table=t, page=t.pages[0])

    _COMPLEMENTARY = {"debit": "credit", "credit": "debit", "paid": None, "discount_rate": None, "discount_amount": None}

    def _missing_values(self, t: LogicalTable, total_ok_rows: set[int]) -> None:
        rows = t.data_row_indices()
        if len(rows) < 3:
            return
        flagged = 0
        for c in range(t.ncols):
            role = t.roles[c]
            if role in self._COMPLEMENTARY:
                continue
            filled = [r for r in rows if t.rows[r][c]]
            ratio = len(filled) / len(rows)
            if ratio < 0.8 or ratio == 1.0:
                continue
            for r in rows:
                if t.rows[r][c]:
                    continue
                flagged += 1
                if r in total_ok_rows:
                    self._add("missing_value", "info", PASSED,
                              f"'{t.columns[c]}' is empty here while {ratio:.0%} of rows have a value. The row total still "
                              "reconciles, so the empty cell is consistent with a genuine blank (kept as empty).",
                              table=t, row=r, col=c)
                else:
                    self._add("missing_value", "warning", FAILED,
                              f"'{t.columns[c]}' is empty here while {ratio:.0%} of rows have a value.",
                              "Confirm whether the source cell is blank or the value was not extracted.", table=t, row=r, col=c)
        if not flagged:
            self._add("missing_value", "passed", PASSED, "No unexpected empty cells in well-populated columns.", table=t,
                      page=t.pages[0])

    def _column_shift(self, t: LogicalTable) -> None:
        """Flag rows whose cells fit the column types much better when moved one column over.

        Checks partial shifts too (e.g. a missing description pushes every later cell one column left).
        """
        from core.value_parser import cell_kind
        typed = t.col_types
        rows = t.data_row_indices()
        # Text columns that (almost) never contain numbers/dates: a number there is a type mismatch.
        strict_text = set()
        for c, ty in enumerate(typed):
            if ty == "text":
                vals = [t.rows[r][c] for r in rows if t.rows[r][c]]
                numeric = sum(cell_kind(v) in ("number", "date") for v in vals)
                if len(vals) >= 3 and numeric <= max(1, 0.1 * len(vals)):  # one outlier may be the shifted cell
                    strict_text.add(c)

        def fits(cell: str, col: int) -> Optional[bool]:
            if not cell:
                return None
            k, ty = cell_kind(cell), typed[col]
            if ty in ("number", "integer", "percent"):
                return k == "number"
            if ty == "date":
                return k == "date"
            if ty == "text":
                return not (col in strict_text and k in ("number", "date"))
            return None

        for r in rows:
            row = t.rows[r]
            bad = [c for c in range(t.ncols) if fits(row[c], c) is False]
            if len(bad) < 2:
                continue
            best = None
            for d in (1, -1):  # observed cell at column c really belongs to column c + d
                for s in range(t.ncols):
                    seg = [c for c in range(s, t.ncols) if row[c] and 0 <= c + d < t.ncols]
                    if len(seg) < 2:
                        continue
                    after = sum(bool(fits(row[c], c + d)) for c in seg) / len(seg)
                    before = sum(bool(fits(row[c], c)) for c in seg) / len(seg)
                    if after >= 0.85 and before <= 0.5:
                        best = (d, s)
                        break
                if best:
                    break
            if best is not None:
                d, s = best
                self._add("column_shift", "warning", FAILED,
                          f"{len(bad)} cells do not match their column types, but would if the cells from "
                          f"'{t.columns[s]}' onwards were moved one column {'right' if d > 0 else 'left'}. "
                          "The row may be misaligned.",
                          "Compare this row with the source and realign the cells if needed.", table=t, row=r, col=s)

    def _duplicates(self, t: LogicalTable, balanced_rows: set[int]) -> None:
        rows = t.data_row_indices()
        exclude = {c for c, role in enumerate(t.roles) if role in ("line_no", "balance")}
        groups: dict[tuple, list[int]] = defaultdict(list)
        for r in rows:
            key = tuple(v for c, v in enumerate(t.rows[r]) if c not in exclude)
            if sum(bool(x) for x in key) >= 3:
                groups[key].append(r)
        found = False
        for key, members in groups.items():
            if len(members) < 2:
                continue
            found = True
            exact = len({tuple(t.rows[r]) for r in members}) == 1
            ids = ", ".join(t.row_meta[r].row_id for r in members)
            if not exact and all(r in balanced_rows for r in members):
                self._add("duplicate_rows", "info", PASSED,
                          f"Rows {ids} look identical apart from the running balance. The balance chain confirms each is a "
                          "separate transaction, so all were kept.", table=t, row=members[1])
            else:
                why = "exactly identical" if exact else "identical apart from line number/balance"
                self._add("duplicate_rows", "warning", FAILED,
                          f"Rows {ids} are {why}. They were kept; nothing was deleted.",
                          "Confirm in the source whether this item genuinely appears more than once.", table=t, row=members[1])
        if not found and rows:
            self._add("duplicate_rows", "passed", PASSED, "No duplicate-looking rows.", table=t, page=t.pages[0])

    def _sequence(self, t: LogicalTable) -> None:
        li = t.role_index("line_no")
        if li is None or t.col_types[li] != "integer":
            return
        nums = [(r, _num(t.values[r][li])) for r in t.data_row_indices()]
        nums = [(r, int(v)) for r, v in nums if v is not None]
        if len(nums) < 3:
            return
        problems = 0
        for (r1, a), (r2, b) in zip(nums, nums[1:]):
            if b == a + 1:
                continue
            problems += 1
            if b > a + 1:
                self._add("sequence_gap", "warning", FAILED,
                          f"Line numbers jump from {a} to {b}: {b - a - 1} row(s) may be missing.",
                          "Check the source for rows that were not extracted.", table=t, row=r2, col=li,
                          extracted=b, expected=a + 1, difference=b - a - 1)
            else:
                self._add("sequence_gap", "warning", FAILED, f"Line number {b} follows {a} (repeated or out of order).",
                          "Check for duplicated or reordered rows.", table=t, row=r2, col=li, extracted=b, expected=a + 1)
        if not problems:
            self._add("sequence_gap", "passed", PASSED, f"Line numbers {nums[0][1]}–{nums[-1][1]} are consecutive.",
                      table=t, page=t.pages[0])


    def _date_order(self, t: LogicalTable) -> None:
        """Dates in a ledger or statement run one way; a single date out of step is often a misread digit."""
        for c, ty in enumerate(t.col_types):
            if ty != "date":
                continue
            seq = [(r, t.values[r][c]) for r in t.data_row_indices() if isinstance(t.values[r][c], date)]
            if len(seq) < 4:
                continue
            pairs = list(zip(seq, seq[1:]))
            up = sum(b >= a for (_, a), (_, b) in pairs)
            down = sum(b <= a for (_, a), (_, b) in pairs)
            ascending = up >= down
            in_order = up if ascending else down
            if in_order / len(pairs) < 0.85:
                continue  # dates are not kept in order in this table, so there is nothing to check
            bad = [(r2, a, b) for (_, a), (r2, b) in pairs if (b < a if ascending else b > a)]
            for r, prev, cur in bad:
                self._add("date_order", "warning", FAILED,
                          f"Date {cur.isoformat()} breaks the {'ascending' if ascending else 'descending'} order "
                          f"(previous row: {prev.isoformat()}).",
                          "A digit may have been misread; compare the date with the source.", table=t, row=r, col=c,
                          extracted=cur.isoformat(), expected=f"{'on or after' if ascending else 'on or before'} {prev.isoformat()}")
            self._passed("date_order", t, len(pairs) - len(bad), len(pairs), f"Dates in '{t.columns[c]}' in order")


def summarise(issues: Iterable[ValidationIssue]) -> dict[str, int]:
    c = Counter(i.severity for i in issues)
    return {k: c.get(k, 0) for k in ("critical", "warning", "info", "passed")}


_VERIFIED = re.compile(r"(\d+) of (\d+) verified")


def check_summary(issues: Iterable[ValidationIssue]) -> list[dict[str, Any]]:
    """Per check: how many items were looked at and how many were correct.

    A passed finding of the form 'n of m verified' counts n correct items; any other passed finding counts one.
    Critical and warning findings each count one item that failed. Informational findings are not counted.
    """
    out: dict[str, dict[str, Any]] = {}
    for i in issues:
        if i.severity == "info":
            continue
        c = out.setdefault(i.check, {"check": i.check, "checked": 0, "passed": 0})
        if i.severity == "passed":
            m = _VERIFIED.search(i.explanation)
            ok = int(m.group(1)) if m else 1
            c["passed"] += ok
            c["checked"] += ok
        else:
            c["checked"] += 1
    return sorted(out.values(), key=lambda c: (c["passed"] == c["checked"], c["check"]))
