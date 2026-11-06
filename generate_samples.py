"""
generate_samples.py  -  InvoiceIQ, Stage 2

Creates realistic sample invoice PDFs in the sample_invoices/ folder.

* Vendor names, vendor IDs, PO numbers and payment terms are READ from
  data/vendors.csv and data/purchase_orders.csv, so the PDFs always match
  the reference data.
* Only PyMuPDF is needed (no other packages).
* The text in every PDF is real, selectable text (no images).

Run it from the project folder:   python generate_samples.py

Note on the rupee sign: the fonts built into PDF files cannot draw the
rupee symbol, so amounts are printed as "Rs." (layout A) or "INR"
(layout B). Both are common on Indian invoices.
"""

import csv
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pymupdf

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUT_DIR = BASE_DIR / "sample_invoices"

GST_RATE = Decimal("0.18")
PAGE_W, PAGE_H = 595, 842          # A4 in points
MARGIN = 40
RIGHT = PAGE_W - MARGIN            # right edge of the content area

DARK = (0.12, 0.14, 0.18)
GREY = (0.40, 0.42, 0.46)
LIGHT = (0.94, 0.95, 0.97)
LINE = (0.80, 0.82, 0.85)
WHITE = (1, 1, 1)

# The company that receives the invoices (the buyer). Fictional.
BUYER = {
    "name": "Meridian Retail Pvt Ltd",
    "address": ["8-2-120, Road No. 2, Banjara Hills", "Hyderabad, Telangana - 500034"],
    "gstin": "36AABCM6670L1Z2",
}

# Extra details for the vendors that issue sample invoices. All fictional.
# (The vendor NAME is not stored here - it comes from vendors.csv.)
VENDOR_PROFILES = {
    "V001": {
        "address": ["Plot 14, Industrial Estate, Kukatpally", "Hyderabad, Telangana - 500072"],
        "gstin": "36AAKCS4821M1Z7",
        "contact": "accounts@sharmaoffice.example  |  +91 40 4000 1101",
        "accent": (0.10, 0.30, 0.55),
    },
    "V003": {
        "address": ["No. 22, Ambattur Industrial Estate", "Chennai, Tamil Nadu - 600058"],
        "gstin": "33AAECK7312P1ZB",
        "contact": "billing@kaverilogistics.example  |  +91 44 4500 2202",
        "accent": (0.55, 0.28, 0.08),
    },
    "V004": {
        "address": ["3rd Floor, Rajajinagar Business Centre", "Bengaluru, Karnataka - 560010"],
        "gstin": "29AAFFD5513R1ZK",
        "contact": "finance@deccanfacility.example  |  +91 80 4100 3303",
        "accent": (0.08, 0.42, 0.34),
    },
    "V010": {
        "address": ["Shop 7, Road No. 12, Banjara Hills", "Hyderabad, Telangana - 500034"],
        "gstin": "36AAHFO2290K1Z3",
        "contact": "orders@orchidcatering.example  |  +91 40 4600 4404",
        "accent": (0.50, 0.15, 0.35),
    },
}

# Two different layouts, so the extractor in Stage 4 sees varied formats.
LAYOUTS = {
    "A": {
        "title": "TAX INVOICE",
        "prefix": "Rs.",
        "bill_to": "BILL TO",
        "meta": {"inv_no": "Invoice No:", "date": "Invoice Date:", "due": "Due Date:",
                 "po": "PO Number:", "terms": "Payment Terms:"},
        "show_currency_row": True,
        "show_hsn": True,
        "columns": ["#", "Description", "HSN/SAC", "Qty", "Rate (Rs.)", "Amount (Rs.)"],
        "totals": ["Subtotal", "GST @ 18%", "Grand Total"],
    },
    "B": {
        "title": "INVOICE",
        "prefix": "INR",
        "bill_to": "Billed To",
        "meta": {"inv_no": "Invoice #", "date": "Date", "due": "Due By",
                 "po": "Customer PO", "terms": "Terms"},
        "show_currency_row": False,
        "show_hsn": False,
        "columns": ["#", "Item / Service", "Units", "Unit Price (INR)", "Line Total (INR)"],
        "totals": ["Sub Total", "GST (18%)", "Amount Due"],
    },
}

# --------------------------------------------------------------------------
# Invoice definitions
# Each item is: (description, HSN/SAC code, quantity, rate)
# The line items add up to the subtotal; GST is 18% of the subtotal
# (rounded to the paisa); total = subtotal + GST.
# "expected_total" is a safety check - the script stops if the maths differs.
# --------------------------------------------------------------------------
NORMAL = {
    "file": "normal_invoice.pdf",
    "vendor_id": "V001",
    "invoice_number": "INV-SS-2026-001",
    "invoice_date": date(2026, 10, 12),
    "po_number": "PO-2026-1001",
    "layout": "A",
    "items": [
        ("A4 Copier Paper 75 GSM (ream of 500)", "4802", 100, "240.00"),
        ("Printer Ink Cartridge - Black", "3215", 12, "650.00"),
        ("Ball Pen Box (pack of 10)", "9608", 60, "70.00"),
    ],
    "expected_total": "42480.00",
}

INVOICES = [
    NORMAL,
    # The duplicate is an exact copy of the normal invoice, only the file name differs.
    {**NORMAL, "file": "duplicate_invoice.pdf"},
    {
        "file": "po_exceeded_invoice.pdf",
        "vendor_id": "V004",
        "invoice_number": "INV-DFS-2026-001",
        "invoice_date": date(2026, 10, 20),
        "po_number": "PO-2026-1004",
        "layout": "B",
        "items": [
            ("Monthly housekeeping service - October", "", 1, "45000.00"),
            ("Deep cleaning of office floors", "", 2, "12500.00"),
            ("Quarterly pest control treatment", "", 1, "10000.00"),
        ],
        "expected_total": "94400.00",
    },
    {
        "file": "missing_po_invoice.pdf",
        "vendor_id": "V010",
        "invoice_number": "INV-OCS-2026-001",
        "invoice_date": date(2026, 10, 25),
        "po_number": None,                       # no PO anywhere on this invoice
        "layout": "B",
        "items": [
            ("Working lunch for team event", "", 100, "150.00"),
            ("Tea and coffee service (per day)", "", 5, "1000.00"),
        ],
        "expected_total": "23600.00",
    },
    {
        "file": "split_invoice_1.pdf",
        "vendor_id": "V003",
        "invoice_number": "SPL-KLF-001",
        "invoice_date": date(2026, 10, 5),
        "po_number": "PO-2026-1003",
        "layout": "A",
        "items": [
            ("Full truck load Hyderabad - Chennai", "996511", 3, "12000.00"),
            ("Loading and unloading charges", "996719", 2, "4500.00"),
            ("Fuel surcharge and detention", "996511", 1, "5847.46"),
        ],
        "expected_total": "60000.00",
    },
    {
        "file": "split_invoice_2.pdf",
        "vendor_id": "V003",
        "invoice_number": "SPL-KLF-002",
        "invoice_date": date(2026, 10, 19),
        "po_number": "PO-2026-1003",
        "layout": "A",
        "items": [
            ("Full truck load Hyderabad - Chennai", "996511", 2, "12000.00"),
            ("Warehousing charges - October", "996729", 1, "6000.00"),
            ("Fuel surcharge and detention", "996511", 1, "3898.31"),
        ],
        "expected_total": "40000.00",
    },
    {
        "file": "split_invoice_3.pdf",
        "vendor_id": "V003",
        "invoice_number": "SPL-KLF-003",
        "invoice_date": date(2026, 10, 30),
        "po_number": "PO-2026-1003",
        "layout": "A",
        "items": [
            ("Full truck load Hyderabad - Chennai", "996511", 3, "12000.00"),
            ("Fuel surcharge and detention", "996511", 1, "6372.88"),
        ],
        "expected_total": "50000.00",
    },
]


# --------------------------------------------------------------------------
# Small helper functions
# --------------------------------------------------------------------------
def load_csv(filename):
    """Read a CSV file from the data folder as a list of dictionaries."""
    with open(DATA_DIR / filename, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def format_inr(amount):
    """Format a Decimal in Indian digit grouping: 118000 -> '1,18,000.00'."""
    whole, paise = f"{amount:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{whole}.{paise}"


ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def below_hundred(n):
    if n < 20:
        return ONES[n]
    return (TENS[n // 10] + " " + ONES[n % 10]).strip()


def amount_in_words(amount):
    """Indian-style amount in words, e.g. 42480.00 -> 'Rupees Forty Two Thousand ... Only'."""
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    parts = []
    for size, name in [(10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")]:
        if rupees >= size:
            parts.append(below_hundred(rupees // size) + " " + name)
            rupees %= size
    if rupees >= 100:
        parts.append(ONES[rupees // 100] + " Hundred")
        rupees %= 100
    if rupees:
        parts.append(below_hundred(rupees))
    words = "Rupees " + " ".join(parts) if parts else "Rupees Zero"
    if paise:
        words += " and " + below_hundred(paise) + " Paise"
    return words + " Only"


def put(page, x, y, s, size=9, bold=False, color=DARK, right=False):
    """Write one piece of text. If right=True, x is the RIGHT edge of the text."""
    font = "hebo" if bold else "helv"
    if right:
        x = x - pymupdf.get_text_length(s, fontname=font, fontsize=size)
    page.insert_text((x, y), s, fontname=font, fontsize=size, color=color)


def calculate(invoice):
    """Work out subtotal, GST and total, and stop if anything looks wrong."""
    subtotal = sum(Decimal(q) * Decimal(r) for _, _, q, r in invoice["items"])
    expected_total = Decimal(invoice["expected_total"])
    gst = expected_total - subtotal
    ideal_gst = (subtotal * GST_RATE).quantize(Decimal("0.01"))
    if abs(gst - ideal_gst) > Decimal("0.01"):
        raise ValueError(f"{invoice['file']}: GST {gst} is not 18% of subtotal {subtotal}")
    return subtotal, gst, expected_total


def check_against_reference_data(invoice, vendors, pos):
    """Make sure the invoice matches vendors.csv and purchase_orders.csv."""
    vendor = vendors.get(invoice["vendor_id"])
    if vendor is None:
        raise ValueError(f"{invoice['file']}: vendor {invoice['vendor_id']} not in vendors.csv")
    po_number = invoice["po_number"]
    if po_number is not None:
        po = pos.get(po_number)
        if po is None:
            raise ValueError(f"{invoice['file']}: {po_number} not in purchase_orders.csv")
        if po["vendor_id"] != invoice["vendor_id"]:
            raise ValueError(f"{invoice['file']}: {po_number} belongs to {po['vendor_id']}")
    return vendor


# --------------------------------------------------------------------------
# PDF drawing
# --------------------------------------------------------------------------
def build_pdf(invoice, vendor, out_path):
    cfg = LAYOUTS[invoice["layout"]]
    profile = VENDOR_PROFILES[invoice["vendor_id"]]
    accent = profile["accent"]
    prefix = cfg["prefix"]
    subtotal, gst, total = calculate(invoice)

    terms_days = int(vendor["payment_terms"].split()[1])          # "Net 30" -> 30
    due_date = invoice["invoice_date"] + timedelta(days=terms_days)

    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    # ---- Header ----------------------------------------------------------
    if invoice["layout"] == "A":
        page.draw_rect(pymupdf.Rect(0, 0, PAGE_W, 74), color=None, fill=accent)
        put(page, MARGIN, 36, vendor["vendor_name"], 17, True, WHITE)
        put(page, MARGIN, 56, "GSTIN: " + profile["gstin"], 9, False, WHITE)
        put(page, RIGHT, 46, cfg["title"], 15, True, WHITE, right=True)
    else:
        put(page, MARGIN, 50, vendor["vendor_name"], 17, True, accent)
        put(page, RIGHT, 50, cfg["title"], 20, True, GREY, right=True)
        page.draw_line((MARGIN, 62), (RIGHT, 62), color=accent, width=2)

    # ---- Vendor address (left) ------------------------------------------
    y = 96
    for line in profile["address"]:
        put(page, MARGIN, y, line, 9, color=GREY)
        y += 12
    if invoice["layout"] == "B":
        put(page, MARGIN, y, "GSTIN: " + profile["gstin"], 9, color=GREY)
        y += 12
    put(page, MARGIN, y, profile["contact"], 8.5, color=GREY)
    left_bottom = y

    # ---- Invoice details box (right) -------------------------------------
    labels = cfg["meta"]
    rows = [
        (labels["inv_no"], invoice["invoice_number"]),
        (labels["date"], invoice["invoice_date"].strftime("%d-%b-%Y")),
        (labels["due"], due_date.strftime("%d-%b-%Y")),
    ]
    if invoice["po_number"]:                       # the missing-PO invoice skips this row
        rows.append((labels["po"], invoice["po_number"]))
    rows.append((labels["terms"], vendor["payment_terms"]))
    if cfg["show_currency_row"]:
        rows.append(("Currency:", "INR"))

    box_top, box_left, row_h = 88, 345, 18
    box_bottom = box_top + row_h * len(rows) + 6
    page.draw_rect(pymupdf.Rect(box_left, box_top, RIGHT, box_bottom), color=LINE, width=0.8)
    ry = box_top + 16
    for label, value in rows:
        put(page, box_left + 8, ry, label, 9, color=GREY)
        put(page, RIGHT - 8, ry, value, 9, True, right=True)
        ry += row_h

    # ---- Bill to ---------------------------------------------------------
    y = max(left_bottom, box_bottom) + 28
    put(page, MARGIN, y, cfg["bill_to"], 8.5, True, GREY)
    y += 15
    put(page, MARGIN, y, BUYER["name"], 10.5, True)
    for line in BUYER["address"]:
        y += 12
        put(page, MARGIN, y, line, 9, color=GREY)
    y += 12
    put(page, MARGIN, y, "GSTIN: " + BUYER["gstin"], 9, color=GREY)

    # ---- Line item table --------------------------------------------------
    y += 30
    if cfg["show_hsn"]:
        # (x position, align right?) for: #, description, HSN, qty, rate, amount
        col_x = [(MARGIN + 6, False), (66, False), (300, False),
                 (400, True), (470, True), (RIGHT - 6, True)]
    else:
        col_x = [(MARGIN + 6, False), (66, False), (385, True), (465, True), (RIGHT - 6, True)]

    page.draw_rect(pymupdf.Rect(MARGIN, y, RIGHT, y + 22), color=None, fill=LIGHT)
    for (x, is_right), heading in zip(col_x, cfg["columns"]):
        put(page, x, y + 15, heading, 8.5, True, GREY, right=is_right)
    y += 22

    for number, (desc, hsn, qty, rate) in enumerate(invoice["items"], start=1):
        amount = Decimal(qty) * Decimal(rate)
        cells = [str(number), desc]
        if cfg["show_hsn"]:
            cells.append(hsn)
        cells += [str(qty), format_inr(Decimal(rate)), format_inr(amount)]
        for (x, is_right), value in zip(col_x, cells):
            put(page, x, y + 16, value, 9, right=is_right)
        y += 24
        page.draw_line((MARGIN, y), (RIGHT, y), color=LINE, width=0.6)

    # ---- Totals -----------------------------------------------------------
    y += 8
    totals_x = 355
    sub_label, gst_label, total_label = cfg["totals"]
    for label, value in [(sub_label, subtotal), (gst_label, gst)]:
        put(page, totals_x, y + 14, label, 9.5, color=GREY)
        put(page, RIGHT - 6, y + 14, f"{prefix} {format_inr(value)}", 9.5, right=True)
        y += 22
    page.draw_rect(pymupdf.Rect(totals_x - 8, y, RIGHT, y + 26), color=None, fill=LIGHT)
    put(page, totals_x, y + 17, total_label, 10.5, True)
    put(page, RIGHT - 6, y + 17, f"{prefix} {format_inr(total)}", 10.5, True, right=True)

    # Amount in words (left of the totals block)
    words_y = y - 40
    put(page, MARGIN, words_y, "Amount in words:", 8.5, True, GREY)
    put(page, MARGIN, words_y + 13, amount_in_words(total), 9)
    y += 26

    # ---- Footer notes -----------------------------------------------------
    y += 34
    put(page, MARGIN, y, "Payment terms: " + vendor["payment_terms"]
        + " - payment due by " + due_date.strftime("%d-%b-%Y") + ".", 9, color=GREY)
    y += 14
    put(page, MARGIN, y, "Please quote the invoice number when making payment.", 9, color=GREY)
    y += 14
    put(page, MARGIN, y, "We declare that this invoice shows the actual price of the goods or "
        "services described.", 8.5, color=GREY)

    sign_y = y + 70
    page.draw_line((RIGHT - 170, sign_y), (RIGHT, sign_y), color=GREY, width=0.6)
    put(page, RIGHT, sign_y + 12, "Authorised Signatory", 9, True, right=True)
    put(page, RIGHT, sign_y + 24, "for " + vendor["vendor_name"], 8.5, color=GREY, right=True)

    put(page, PAGE_W / 2 - 160, PAGE_H - 28,
        "Sample invoice created for InvoiceIQ testing. All names, numbers and GSTINs are fictional.",
        7.5, color=GREY)

    doc.set_metadata({"title": "Invoice " + invoice["invoice_number"],
                      "author": vendor["vendor_name"], "creator": "InvoiceIQ sample generator"})
    doc.save(str(out_path))
    doc.close()
    return subtotal, gst, total


# --------------------------------------------------------------------------
# Main: build every PDF, then re-open each one and prove the text is readable
# --------------------------------------------------------------------------
def read_pdf_text(path):
    with pymupdf.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


def main():
    vendors = {row["vendor_id"]: row for row in load_csv("vendors.csv")}
    pos = {row["po_number"]: row for row in load_csv("purchase_orders.csv")}
    OUT_DIR.mkdir(exist_ok=True)

    print(f"Creating {len(INVOICES)} sample invoices in {OUT_DIR}\n")
    summary = []
    for invoice in INVOICES:
        vendor = check_against_reference_data(invoice, vendors, pos)
        out_path = OUT_DIR / invoice["file"]
        subtotal, gst, total = build_pdf(invoice, vendor, out_path)
        summary.append((invoice, vendor, out_path, subtotal, gst, total))

    # ---- Self-test: read every PDF back and check the text ---------------
    print(f"{'File':<24}{'Invoice No':<19}{'PO':<14}{'Subtotal':>12}{'GST':>11}{'Total':>12}  Check")
    all_ok = True
    texts = {}
    for invoice, vendor, out_path, subtotal, gst, total in summary:
        text = read_pdf_text(out_path)
        texts[invoice["file"]] = text
        problems = []
        if len(text.strip()) < 100:
            problems.append("no extractable text")
        if invoice["invoice_number"] not in text:
            problems.append("invoice number missing")
        if vendor["vendor_name"] not in text:
            problems.append("vendor name missing")
        if format_inr(total) not in text:
            problems.append("total missing")
        if invoice["po_number"] is None:
            if re.search(r"\bPO\b|PO-\d|purchase\s+order", text, re.IGNORECASE):
                problems.append("a PO reference appears but should not")
        elif invoice["po_number"] not in text:
            problems.append("PO number missing")
        status = "OK" if not problems else "FAIL: " + ", ".join(problems)
        all_ok = all_ok and not problems
        print(f"{invoice['file']:<24}{invoice['invoice_number']:<19}"
              f"{(invoice['po_number'] or '(none)'):<14}{format_inr(subtotal):>12}"
              f"{format_inr(gst):>11}{format_inr(total):>12}  {status}")

    same = texts["normal_invoice.pdf"] == texts["duplicate_invoice.pdf"]
    print(f"\nDuplicate text identical to normal invoice: {same}")
    all_ok = all_ok and same
    print("All checks passed." if all_ok else "Some checks FAILED - see above.")


if __name__ == "__main__":
    main()