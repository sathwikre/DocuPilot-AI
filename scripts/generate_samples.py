"""Generate synthetic sample documents with exact ground truth.

All data is synthetic. Every number is computed with Decimal arithmetic here and written both
into the PDF and into samples/expected/<name>.json, so tests can compare extraction cell by cell.

Usage:  python scripts/generate_samples.py
"""
from __future__ import annotations

import io
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional, Sequence

import pymupdf as fitz
import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
EXPECTED = SAMPLES / "expected"

D = Decimal
CENT = D("0.01")


def money(v: Decimal) -> str:
    """1234.5 -> '1,234.50'; negatives with a leading minus."""
    q = v.quantize(CENT, rounding=ROUND_HALF_UP)
    s = f"{abs(q):,.2f}"
    return f"-{s}" if q < 0 else s


def r2(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- drawing helper

def draw_table(c: canvas.Canvas, x: float, y_top: float, widths: Sequence[float], header: Sequence[str],
               rows: Sequence[Sequence[str]], *, aligns: Sequence[str], style: str = "grid", font: str = "Helvetica",
               size: float = 8, row_h: float = 15, header_fill: Optional[tuple] = None, draw_header: bool = True) -> float:
    """Draw a table; cells containing '\\n' wrap onto extra lines. Returns the y below the table."""
    lead = size + 2.5
    total_w = sum(widths)
    xs = [x]
    for w in widths:
        xs.append(xs[-1] + w)
    y = y_top
    row_bounds = []

    def draw_row(cells, bold=False):
        nonlocal y
        nlines = max(len(str(cell).split("\n")) for cell in cells)
        h = row_h + (nlines - 1) * lead
        if header_fill and bold:
            c.setFillColorRGB(*header_fill)
            c.rect(x, y - h, total_w, h, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
        c.setFont({"Times-Roman": "Times-Bold"}.get(font, font + "-Bold") if bold else font, size)
        for i, cell in enumerate(cells):
            for li, part in enumerate(str(cell).split("\n")):
                ty = y - 4 - size + 1 - li * lead
                a = "left" if bold and aligns[i] == "right" and False else aligns[i]
                if a == "right":
                    c.drawRightString(xs[i + 1] - 4, ty, part)
                elif a == "center":
                    c.drawCentredString((xs[i] + xs[i + 1]) / 2, ty, part)
                else:
                    c.drawString(xs[i] + 4, ty, part)
        row_bounds.append((y, y - h))
        y -= h

    if draw_header:
        draw_row(header, bold=True)
    for r in rows:
        draw_row(r)
    c.setLineWidth(0.6)
    if style == "grid":
        for top, bottom in row_bounds:
            c.line(x, top, x + total_w, top)
        c.line(x, y, x + total_w, y)
        for xv in xs:
            c.line(xv, y_top, xv, y)
    elif style == "hlines":
        c.line(x, y_top, x + total_w, y_top)
        if draw_header and row_bounds:
            c.line(x, row_bounds[0][1], x + total_w, row_bounds[0][1])
        c.line(x, y, x + total_w, y)
    return y


def flat(cell: str) -> str:
    return " ".join(str(cell).split())


# --------------------------------------------------------------------------- Sample A: invoice

INVOICE_ITEMS = [
    # sku, description, hsn, qty, unit, price, disc%, tax%
    ("FX-1001", "Hex bolt M8 x 40, zinc plated", "7318", 500, "pcs", "0.42", 0, 18),
    ("FX-1002", "Hex nut M8, zinc plated", "7318", 500, "pcs", "0.11", 0, 18),
    ("FX-2040", "Industrial fastener kit,\nassorted sizes (240 pc)", "7318", 12, "kit", "38.50", 5, 18),
    ("TL-0310", "Cordless drill 18V, 2 batteries", "8467", 4, "ea", "129.99", 10, 18),
    ("TL-0311", "Drill bit set HSS 19 pc", "8207", 6, "set", "24.75", 0, 18),
    ("SF-5500", "Safety goggles, anti-fog", "9004", 40, "pcs", "6.20", 0, 12),
    ("SF-5510", "Nitrile gloves, box of 100", "4015", 25, "box", "11.90", 2.5, 12),
    ("EL-7002", "Cable tie 300mm, pack of 100", "3926", 30, "pack", "3.45", 0, 18),
    ("EL-7105", "Insulation tape 19mm x 20m", "3919", 60, "roll", "0.95", 0, 18),
    ("PK-9000", "Stretch film 500mm x 300m", "3920", 8, "roll", "17.60", 0, 18),
    ("PK-9010", "Corrugated box 40x30x30,\ndouble wall", "4819", 150, "pcs", "1.35", 3, 12),
    ("TL-0420", "Torque wrench 1/2in, 40-210 Nm", "8204", 2, "ea", "89.00", 0, 18),
    ("SV-0001", "On-site calibration service", "9987", 1, "job", "150.00", 0, 18),
    ("RT-0310", "Return: cordless drill (damaged)", "8467", -1, "ea", "129.99", 10, 18),
]
INVOICE_ERROR_ROW = 7  # 1-based line number whose Line Total is deliberately overstated
INVOICE_ERROR_DELTA = D("10.00")
SHIPPING = D("45.00")


def build_invoice(introduce_error: bool = True) -> tuple[bytes, dict]:
    header = ["#", "SKU", "Description", "HSN", "Qty", "Unit", "Unit Price", "Disc %", "Net Amount",
              "Tax %", "Tax Amount", "Line Total"]
    rows, truth_rows = [], []
    sub = tax_total = D(0)
    for i, (sku, desc, hsn, qty, unit, price, disc, tax) in enumerate(INVOICE_ITEMS, start=1):
        p = D(price)
        gross = D(qty) * p
        net = r2(gross * (1 - D(str(disc)) / 100))
        tax_amt = r2(net * D(tax) / 100)
        line_total = net + tax_amt
        sub += net
        tax_total += tax_amt
        printed_total = line_total + (INVOICE_ERROR_DELTA if introduce_error and i == INVOICE_ERROR_ROW else 0)
        disc_s = f"{D(str(disc)):.1f}%" if disc else ""
        row = [str(i), sku, desc, hsn, str(qty), unit, f"{p:,.2f}", disc_s, money(net), f"{tax}%",
               money(tax_amt), money(printed_total)]
        rows.append(row)
        truth_rows.append([flat(c) for c in row])
    grand = sub + tax_total + SHIPPING

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    c.setTitle("Sample A - Invoice INV-2024-0457")
    W, H = landscape(A4)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(36, H - 50, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    c.drawString(36, H - 68, "Brightline Industrial Supplies Ltd  |  14 Foundry Road, Leeds LS9 8AB  |  VAT GB 123 4567 89")
    c.drawString(36, H - 90, "Invoice No: INV-2024-0457")
    c.drawString(36, H - 103, "Invoice Date: 18 Mar 2024")
    c.drawString(36, H - 116, "Due Date: 17 Apr 2024")
    c.drawString(430, H - 90, "Bill To: Northwind Traders LLC")
    c.drawString(430, H - 103, "220 Harbour Street, Bristol BS1 5TT")
    c.drawString(430, H - 116, "PO Number: PO-88213")
    widths = [24, 58, 168, 42, 36, 34, 58, 44, 66, 38, 62, 70]
    aligns = ["center", "left", "left", "left", "right", "left", "right", "right", "right", "right", "right", "right"]
    y = draw_table(c, 36, H - 135, widths, header, rows, aligns=aligns, style="grid", size=8, row_h=15,
                   header_fill=(0.88, 0.9, 0.96))
    x_lab, x_val = 560, 36 + sum(widths) - 4
    c.setFont("Helvetica", 9)
    for label, val in (("Subtotal", sub), ("Tax Total", tax_total), ("Shipping", SHIPPING)):
        y -= 16
        c.drawString(x_lab, y, label)
        c.drawRightString(x_val, y, money(val))
    y -= 18
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x_lab, y, "Grand Total")
    c.drawRightString(x_val, y, money(grand))
    c.setFont("Helvetica", 8)
    c.drawString(36, 40, "Payment terms: 30 days. Synthetic sample document generated for DocuPilot AI testing.")
    c.showPage()
    c.save()

    expected = {
        "name": "sample_a_invoice",
        "description": "Digital invoice, 12 columns, ruled grid, wrapped descriptions, a negative return line, "
                       "and a deliberately overstated Line Total on line 7.",
        "doc_type": "invoice",
        "pages": 1,
        "scanned": False,
        "tables": [{"title": None, "pages": [1], "header": header, "rows": truth_rows}],
        "fields": {"subtotal": str(sub), "tax_total": str(tax_total), "shipping": str(SHIPPING), "grand_total": str(grand)},
        "expected_issues": ([
            {"check": "line_arithmetic", "table": 0, "row": INVOICE_ERROR_ROW, "column": "Line Total",
             "difference": str(INVOICE_ERROR_DELTA)},
            {"check": "line_total_sum", "table": 0, "row": None, "difference": str(INVOICE_ERROR_DELTA)},
        ] if introduce_error else []),
    }
    return buf.getvalue(), expected


# --------------------------------------------------------------------------- Sample B: scanned statement

def _statement_transactions() -> tuple[D, list[dict]]:
    rng = np.random.default_rng(7)
    opening = D("12500.00")
    descs = [
        ("POS/GREENLEAF GROCERS", "debit"), ("UPI/COFFEE HOUSE", "debit"), ("NEFT/SALARY MARCH", "credit"),
        ("ATM WDL/CITY CENTRE", "debit"), ("ACH/ELECTRIC UTILITY CO", "debit"), ("IMPS/REFUND ORDER 7712", "credit"),
        ("POS/OFFICE DEPOT", "debit"), ("CHQ DEP/0045521", "credit"), ("CARD/AIRLINE TICKETS", "debit"),
        ("NEFT/CLIENT PAYMENT INV 2231", "credit"), ("POS/FUEL STATION 22", "debit"), ("ACH/INSURANCE PREMIUM", "debit"),
    ]
    txns = []
    bal = opening
    day = 1
    n = 44
    for i in range(n):
        if i % 3 == 0 and day < 30:
            day += 1
        desc, kind = descs[int(rng.integers(0, len(descs)))]
        amt = D(str(round(float(rng.uniform(8, 900 if kind == "debit" else 2500)), 2))).quantize(CENT)
        txns.append({"day": day, "desc": desc, "kind": kind, "amt": amt, "ref": f"{int(rng.integers(100000, 999999))}"})
    # Legitimate duplicate-looking pair: same day, description, ref style and amount.
    txns.insert(9, {"day": txns[8]["day"], "desc": "UPI/COFFEE HOUSE", "kind": "debit", "amt": D("4.50"), "ref": "-"})
    txns.insert(10, {"day": txns[8]["day"], "desc": "UPI/COFFEE HOUSE", "kind": "debit", "amt": D("4.50"), "ref": "-"})
    # Wrapped description (within a page) and a description split across the page 2 -> 3 boundary.
    txns[4]["desc"] = "NEFT/BRIGHTLINE INDUSTRIAL\nSUPPLIES LTD INV 0457"
    txns[4]["kind"] = "debit"
    for t in txns:
        bal = bal + t["amt"] if t["kind"] == "credit" else bal - t["amt"]
        t["bal"] = bal
    return opening, txns


def build_statement(scanned: bool = True, dpi: int = 150) -> tuple[bytes, dict]:
    opening, txns = _statement_transactions()
    header = ["Txn Date", "Value Date", "Description", "Ref No", "Debit", "Credit", "Balance"]
    widths = [56, 56, 160, 62, 60, 60, 70]
    aligns = ["left", "left", "left", "left", "right", "right", "right"]
    W, H = A4
    per_page = [17, 19, len(txns) - 36]
    boundary_row = 35  # index of last row on page 2, whose description continues on page 3
    txns[boundary_row]["desc"] = "NEFT/ACME LOGISTICS PVT LTD\nFREIGHT INV 4471"
    txns[boundary_row]["kind"] = "debit"
    # Recompute balances after edits.
    bal = opening
    for t in txns:
        bal = bal + t["amt"] if t["kind"] == "credit" else bal - t["amt"]
        t["bal"] = bal
    truth_rows = []

    def cells(t):
        d = f"{t['day']:02d}/03/2024"
        vd = f"{min(t['day'] + (1 if t['kind'] == 'credit' else 0), 31):02d}/03/2024"
        return [d, vd, t["desc"], t["ref"], money(t["amt"]) if t["kind"] == "debit" else "",
                money(t["amt"]) if t["kind"] == "credit" else "", money(t["bal"])]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Sample B - Account statement")
    idx = 0
    for p, count in enumerate(per_page, start=1):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(36, H - 48, "HARBOURSIDE BANK  -  ACCOUNT STATEMENT")
        c.setFont("Helvetica", 9)
        top = H - 70
        if p == 1:
            c.drawString(36, H - 70, "Account Holder: Northwind Traders LLC")
            c.drawString(36, H - 83, "Account Number: XXXX-XXXX-4821")
            c.drawString(36, H - 96, "Statement Period: 01/03/2024 to 31/03/2024")
            c.drawString(36, H - 109, "Opening Balance: " + money(opening))
            top = H - 128
        page_rows = []
        for t in txns[idx: idx + count]:
            row = cells(t)
            if p == 2 and t is txns[boundary_row]:
                first, second = row[2].split("\n")
                row = row[:2] + [first] + row[3:]
            page_rows.append(row)
        if p == 3:
            page_rows.insert(0, ["", "", txns[boundary_row]["desc"].split("\n")[1], "", "", "", ""])
        y = draw_table(c, 36, top, widths, header, page_rows, aligns=aligns, style="hlines", size=8.5,
                       row_h=15.5, header_fill=(0.85, 0.85, 0.85))
        if p == 3:
            debits = sum((t["amt"] for t in txns if t["kind"] == "debit"), D(0))
            credits = sum((t["amt"] for t in txns if t["kind"] == "credit"), D(0))
            c.setFont("Helvetica", 9)
            for label, v in (("Total Debits", debits), ("Total Credits", credits), ("Closing Balance", txns[-1]["bal"])):
                y -= 16
                c.drawString(330, y, label + ":")
                c.drawRightString(36 + sum(widths) - 4, y, money(v))
        c.setFont("Helvetica", 7.5)
        c.drawCentredString(W / 2, 30, f"Page {p} of {len(per_page)}  -  synthetic sample for DocuPilot AI")
        c.showPage()
        idx += count
    c.save()
    for t in txns:
        truth_rows.append([flat(x) for x in cells(t)])
    pdf = buf.getvalue()
    if scanned:
        pdf = rasterize(pdf, dpi=dpi, skew={2: 0.6}, seed=11)
    expected = {
        "name": "sample_b_statement_scanned",
        "description": "3-page scanned bank statement (image-only PDF with noise, page 2 skewed 0.6°), repeated "
                       "headers, a wrapped description, a row split across the page 2-3 boundary, and two "
                       "legitimate identical-looking coffee purchases.",
        "doc_type": "bank_statement",
        "pages": 3,
        "scanned": scanned,
        "tables": [{"title": None, "pages": [1, 2, 3], "header": header, "rows": truth_rows}],
        "fields": {"opening_balance": str(opening), "closing_balance": str(txns[-1]["bal"]),
                   "total_debits": str(sum((t["amt"] for t in txns if t["kind"] == "debit"), D(0))),
                   "total_credits": str(sum((t["amt"] for t in txns if t["kind"] == "credit"), D(0)))},
        "legit_duplicate_rows": [10, 11],
        "expected_issues": [],
    }
    return pdf, expected


def rasterize(pdf: bytes, dpi: int = 150, skew: Optional[dict] = None, seed: int = 0, noise: float = 9.0) -> bytes:
    """Turn a digital PDF into an image-only 'scanned' PDF (grayscale, noise, blur, optional skew)."""
    rng = np.random.default_rng(seed)
    src = fitz.open(stream=pdf, filetype="pdf")
    out = fitz.open()
    for i, page in enumerate(src, start=1):
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        if skew and i in skew:
            img = img.rotate(skew[i], resample=Image.BICUBIC, fillcolor=255, expand=False)
        arr = np.asarray(img).astype(np.float32)
        arr += rng.normal(0, noise, arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5))
        jb = io.BytesIO()
        img.save(jb, format="JPEG", quality=82)
        np_page = out.new_page(width=page.rect.width, height=page.rect.height)
        np_page.insert_image(np_page.rect, stream=jb.getvalue())
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    src.close()
    return data


# --------------------------------------------------------------------------- Sample C: complex report

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
REPORT_ERROR_REGION = "South-East"
REPORT_ERROR_DELTA = D("10.0")


def _fmt1(v: Optional[Decimal]) -> str:
    if v is None:
        return ""
    return f"({abs(v):,.1f})" if v < 0 else f"{v:,.1f}"


def build_report() -> tuple[bytes, dict]:
    rng = np.random.default_rng(3)
    regions = ["North", "North-East", "East", "South-East", "South", "South-West", "West", "North-West",
               "Central", "Metro A", "Metro B", "Coastal", "Highlands", "Islands", "Lakes", "Plains",
               "Valley", "Border", "New Markets", "Online"]
    t1_header = ["Region"] + MONTHS + ["FY Total"]
    t1_rows = []
    for r in regions:
        vals: list[Optional[Decimal]] = []
        for mi in range(12):
            if r == "New Markets" and mi < 3:
                vals.append(None)  # region launched in April: legitimately empty cells
                continue
            v = D(str(round(float(rng.uniform(40, 260)), 1)))
            if r == "Islands" and mi in (1, 7):
                v = -D(str(round(float(rng.uniform(2, 15)), 1)))  # net returns -> negative, shown in parentheses
            vals.append(v)
        total = sum((v for v in vals if v is not None), D(0))
        if r == REPORT_ERROR_REGION:
            total += REPORT_ERROR_DELTA
        t1_rows.append([r] + [_fmt1(v) for v in vals] + [_fmt1(total)])

    t2_header = ["SKU", "Item", "Category", "Warehouse", "On Hand", "Reserved", "Available", "Reorder Level",
                 "Unit Cost", "Stock Value"]
    items = [("WB-100", "Widget basic", "Widgets", "LDS"), ("WB-200", "Widget pro", "Widgets", "LDS"),
             ("GX-310", "Gear 31T", "Gears", "MAN"), ("GX-320", "Gear 32T", "Gears", "MAN"),
             ("BR-010", "Bearing 10mm", "Bearings", "BRS"), ("BR-012", "Bearing 12mm", "Bearings", "BRS"),
             ("SP-500", "Spring coil", "Springs", "LDS"), ("SP-510", "Spring leaf", "Springs", "MAN"),
             ("CL-001", "Clamp small", "Clamps", "BRS"), ("CL-002", "Clamp large", "Clamps", "LDS")]
    t2_rows = []
    for i, (sku, item, cat, wh) in enumerate(items):
        on_hand = int(rng.integers(40, 900))
        reserved = int(rng.integers(0, 40))
        cost = D(str(round(float(rng.uniform(0.5, 40)), 2))).quantize(CENT)
        avail = on_hand - reserved
        reorder = "" if i in (2, 6, 9) else str(int(rng.integers(50, 200)))
        t2_rows.append([sku, item, cat, wh, f"{on_hand:,}", f"{reserved:,}", f"{avail:,}", reorder, f"{cost:,.2f}",
                        money(cost * avail)])

    t3_header = ["Vendor", "Invoice No", "Invoice Date", "Due Date", "Terms", "Currency", "Amount", "Paid",
                 "Outstanding", "Status"]
    vendors = ["Apex Metals", "Brightline Ind.", "Cobalt Freight", "Delta Plastics", "Apex Metals", "Evergreen Pack",
               "Fulton Tools", "Granite Services", "Cobalt Freight"]
    t3_rows = []
    for i, v in enumerate(vendors):
        amt = D(str(round(float(rng.uniform(300, 9000)), 2))).quantize(CENT)
        paid = None if i in (1, 5, 8) else r2(amt * D(str([1, 0.5, 0.25, 1, 0.4, 1][i % 6])))
        out = amt - (paid or D(0))
        status = "Paid" if out == 0 else ("Open" if paid is None else "Partial")
        t3_rows.append([v, f"VI-{5100 + i * 7}", f"{(i % 27) + 1:02d}/02/2024", f"{(i % 27) + 1:02d}/03/2024",
                        "Net 30", "USD", money(amt), money(paid) if paid is not None else "", money(out), status])

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    c.setTitle("Sample C - Quarterly operations report")
    W, H = landscape(A4)
    t1_widths = [72] + [48] * 12 + [64]
    t1_aligns = ["left"] + ["right"] * 13
    # Page 1
    c.setFont("Helvetica-Bold", 18)
    c.drawString(36, H - 50, "Operations Report FY2024")
    c.setFont("Helvetica", 9)
    c.drawString(36, H - 68, "Prepared by Finance Analytics. Figures in USD thousands. Synthetic data for DocuPilot AI testing.")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(36, H - 96, "Table 1: Regional Sales by Month")
    y = draw_table(c, 36, H - 104, t1_widths, t1_header, t1_rows[:13], aligns=t1_aligns, style="grid", size=7.5,
                   row_h=14, header_fill=(0.9, 0.92, 0.97))
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(W / 2, 28, "Page 1 of 4")
    c.showPage()
    # Page 2: continuation of Table 1, then borderless Table 2
    c.setFont("Helvetica-Bold", 11)
    c.drawString(36, H - 50, "Table 1: Regional Sales by Month (continued)")
    y = draw_table(c, 36, H - 58, t1_widths, t1_header, t1_rows[13:], aligns=t1_aligns, style="grid", size=7.5,
                   row_h=14, header_fill=(0.9, 0.92, 0.97))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(36, y - 34, "Table 2: Inventory Snapshot (31 Dec 2024)")
    t2_widths = [64, 92, 72, 70, 62, 62, 62, 80, 64, 80]
    t2_aligns = ["left", "left", "left", "left", "right", "right", "right", "right", "right", "right"]
    draw_table(c, 36, y - 44, t2_widths, t2_header, t2_rows, aligns=t2_aligns, style="none", font="Times-Roman",
               size=9, row_h=16)
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(W / 2, 28, "Page 2 of 4")
    c.showPage()
    # Page 3: an unrelated 10-column table with horizontal rules only (must NOT merge with Table 2)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(36, H - 50, "Table 3: Vendor Payment Schedule")
    t3_widths = [92, 64, 70, 66, 50, 52, 76, 76, 80, 56]
    t3_aligns = ["left", "left", "left", "left", "left", "left", "right", "right", "right", "left"]
    y = draw_table(c, 36, H - 60, t3_widths, t3_header, t3_rows, aligns=t3_aligns, style="hlines", size=8.5,
                   row_h=16)
    c.setFont("Helvetica", 9)
    c.drawString(36, y - 30, "Outstanding amounts are due on the stated due dates. Partial payments were agreed with vendors in writing.")
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(W / 2, 28, "Page 3 of 4")
    c.showPage()
    # Page 4: narrative only
    c.setFont("Helvetica-Bold", 13)
    c.drawString(36, H - 50, "Commentary")
    c.setFont("Helvetica", 10)
    text = ("Sales grew in most regions during the second half. The Islands region recorded net returns in February "
            "and August, shown as negative values in parentheses. New Markets launched in April, so the first quarter "
            "is intentionally blank. Inventory levels remain within policy except where reorder levels are not yet "
            "defined. This page intentionally contains no tables.")
    yy = H - 76
    line = ""
    for word in text.split():
        if c.stringWidth(line + " " + word, "Helvetica", 10) > W - 72:
            c.drawString(36, yy, line.strip())
            yy -= 14
            line = ""
        line += " " + word
    c.drawString(36, yy, line.strip())
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(W / 2, 28, "Page 4 of 4")
    c.showPage()
    c.save()

    err_row = regions.index(REPORT_ERROR_REGION) + 1
    expected = {
        "name": "sample_c_report",
        "description": "4-page report: a 14-column ruled table continued across pages 1-2 with a repeated header, "
                       "a borderless 10-column table with empty cells, an unrelated 10-column table on page 3 that "
                       "must not be merged, negative numbers in parentheses, and a narrative page without tables. "
                       f"The FY Total for {REPORT_ERROR_REGION} is deliberately overstated by {REPORT_ERROR_DELTA}.",
        "doc_type": "financial_report",
        "pages": 4,
        "scanned": False,
        "tables": [
            {"title": "Table 1: Regional Sales by Month", "pages": [1, 2], "header": t1_header, "rows": t1_rows},
            {"title": "Table 2: Inventory Snapshot (31 Dec 2024)", "pages": [2], "header": t2_header, "rows": t2_rows},
            {"title": "Table 3: Vendor Payment Schedule", "pages": [3], "header": t3_header, "rows": t3_rows},
        ],
        "fields": {},
        "expected_issues": [
            {"check": "row_total", "table": 0, "row": err_row, "column": "FY Total", "difference": str(REPORT_ERROR_DELTA)},
        ],
    }
    return buf.getvalue(), expected


# --------------------------------------------------------------------------- extra fixtures for tests

def build_long_statement(pages: int) -> bytes:
    """A digital statement-like document with one table row block per page (for page-limit tests)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    bal = D("1000.00")
    header = ["Date", "Description", "Debit", "Credit", "Balance"]
    for p in range(1, pages + 1):
        rows = []
        for i in range(10):
            amt = D(f"{(p * 7 + i * 3) % 50 + 1}.25")
            credit = (p + i) % 4 == 0
            bal = bal + amt if credit else bal - amt
            rows.append([f"{(i % 28) + 1:02d}/01/2024", f"Item {p}-{i}", "" if credit else money(amt),
                         money(amt) if credit else "", money(bal)])
        draw_table(c, 50, H - 60, [70, 200, 70, 70, 80], header, rows, aligns=["left", "left", "right", "right", "right"],
                   style="grid", size=9)
        c.showPage()
    c.save()
    return buf.getvalue()


def write_all() -> list[Path]:
    SAMPLES.mkdir(exist_ok=True)
    EXPECTED.mkdir(exist_ok=True)
    out = []
    for fname, (pdf, exp) in {
        "sample_a_invoice.pdf": build_invoice(),
        "sample_b_statement_scanned.pdf": build_statement(),
        "sample_c_report.pdf": build_report(),
    }.items():
        (SAMPLES / fname).write_bytes(pdf)
        (EXPECTED / fname.replace(".pdf", ".json")).write_text(json.dumps(exp, indent=2), encoding="utf-8")
        out.append(SAMPLES / fname)
    # A scanned image version of the invoice (PNG) to exercise image upload + grid OCR.
    doc = fitz.open(stream=build_invoice()[0], filetype="pdf")
    pix = doc[0].get_pixmap(dpi=170, colorspace=fitz.csGRAY)
    (SAMPLES / "sample_d_invoice_photo.png").write_bytes(pix.tobytes("png"))
    out.append(SAMPLES / "sample_d_invoice_photo.png")
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    for p in write_all():
        print("wrote", p.relative_to(ROOT))
