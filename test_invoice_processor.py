"""
test_invoice_processor.py  -  checks invoice_processor.py (InvoiceIQ, Stage 4)

Run:   python test_invoice_processor.py

Uses a throw-away database in a temporary folder.  Your real invoice_history.db
is never written to (the test also proves that by comparing its checksum).
"""
import hashlib
import tempfile
from decimal import Decimal
from pathlib import Path

import database as db
import invoice_processor as ip

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "sample_invoices"
REAL_DB = db.DB_PATH            # remember the real database path before we swap it out
passed = failed = 0


def check(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print("  PASS ", label)
    else:
        failed += 1
        print("  FAIL ", label)


def section(title):
    print(f"\n{title}")


def fresh_db():
    """Empty the TEMPORARY database. Refuses to touch the real one."""
    assert db.DB_PATH != REAL_DB, "Safety stop: tests must never use the real database."
    db.reset_database()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def fields(**overrides):
    """A clean, valid invoice (Sharma Office Supplies, PO-2026-1001) to tweak per test."""
    base = {
        "vendor_name": "Sharma Office Supplies Pvt Ltd", "vendor_name_signature": None,
        "invoice_number": "INV-T-001", "invoice_date": "2026-10-12", "due_date": "2026-11-11",
        "po_number": "PO-2026-1001", "payment_terms": "Net 30",
        "subtotal": Decimal("10000.00"), "tax_amount": Decimal("1800.00"),
        "total_amount": Decimal("11800.00"), "currency": "INR",
        "extraction_notes": [], "raw_text": "x", "extraction_error": None,
    }
    base.update(overrides)
    return base


def submit(f, name="synthetic.pdf"):
    """Evaluate + save a synthetic invoice, return (evaluation, processing_id)."""
    ev = ip.evaluate_invoice(f)
    return ev, ip.save_evaluation(name, f, ev)


# --------------------------------------------------------------------------
def test_extraction_all_seven_layouts():
    section("1. Extraction from all seven sample PDFs")
    expected = {
        "normal_invoice.pdf": ("Sharma Office Supplies Pvt Ltd", "INV-SS-2026-001", "2026-10-12", "PO-2026-1001", "36000.00", "6480.00", "42480.00"),
        "duplicate_invoice.pdf": ("Sharma Office Supplies Pvt Ltd", "INV-SS-2026-001", "2026-10-12", "PO-2026-1001", "36000.00", "6480.00", "42480.00"),
        "po_exceeded_invoice.pdf": ("Deccan Facility Services LLP", "INV-DFS-2026-001", "2026-10-20", "PO-2026-1004", "80000.00", "14400.00", "94400.00"),
        "missing_po_invoice.pdf": ("Orchid Catering Services", "INV-OCS-2026-001", "2026-10-25", None, "20000.00", "3600.00", "23600.00"),
        "split_invoice_1.pdf": ("Kaveri Logistics & Freight Pvt Ltd", "SPL-KLF-001", "2026-10-05", "PO-2026-1003", "50847.46", "9152.54", "60000.00"),
        "split_invoice_2.pdf": ("Kaveri Logistics & Freight Pvt Ltd", "SPL-KLF-002", "2026-10-19", "PO-2026-1003", "33898.31", "6101.69", "40000.00"),
        "split_invoice_3.pdf": ("Kaveri Logistics & Freight Pvt Ltd", "SPL-KLF-003", "2026-10-30", "PO-2026-1003", "42372.88", "7627.12", "50000.00"),
    }
    for filename, (vendor, number, date, po, sub, tax, total) in expected.items():
        f = ip.extract_invoice_fields(SAMPLES / filename)
        got = (f["vendor_name"], f["invoice_number"], f["invoice_date"], f["po_number"],
               str(f["subtotal"]) if f["subtotal"] is not None else None,
               str(f["tax_amount"]) if f["tax_amount"] is not None else None,
               str(f["total_amount"]) if f["total_amount"] is not None else None)
        check(got == (vendor, number, date, po, sub, tax, total), f"{filename}: all 7 fields extracted correctly")
        check(not f["extraction_notes"] and f["currency"] == "INR", f"{filename}: no extraction warnings, currency INR")
    f = ip.extract_invoice_fields(SAMPLES / "normal_invoice.pdf")
    check(f["total_amount"] != f["subtotal"], "Grand Total is not mistaken for Subtotal")
    check(f["due_date"] == "2026-11-11", "due date extracted")


def test_end_to_end_sample_pdfs():
    section("2. End-to-end decisions on the sample PDFs (same database, in order)")
    fresh_db()
    a = ip.process_invoice(SAMPLES / "normal_invoice.pdf")
    check(a["decision"] == db.DECISION_APPROVED, "normal invoice is APPROVED")
    check(db.get_po_consumed("PO-2026-1001") == 42480.0, "approved invoice consumes PO-2026-1001 (42,480)")

    b = ip.process_invoice(SAMPLES / "duplicate_invoice.pdf")
    check(b["decision"] == db.DECISION_REJECTED, "second submission is REJECTED")
    check("Duplicate" in b["explanation"], "rejection reason says duplicate")
    check(b["record"]["duplicate_of"] == a["processing_id"], "duplicate points to the original record")
    check(db.get_po_consumed("PO-2026-1001") == 42480.0, "duplicate does NOT consume extra PO balance")

    c = ip.process_invoice(SAMPLES / "po_exceeded_invoice.pdf")
    check(c["decision"] == db.DECISION_REJECTED and "tolerance" in c["explanation"],
          "94,400 vs PO 75,000 + 2% (76,500) is REJECTED for exceeding tolerance")
    check(db.get_po_consumed("PO-2026-1004") == 0.0, "rejected invoice consumes nothing")

    d = ip.process_invoice(SAMPLES / "missing_po_invoice.pdf")
    check(d["decision"] == db.DECISION_FLAGGED and "PO number is missing" in d["explanation"],
          "invoice with no PO is FLAGGED FOR REVIEW")
    check(d["record"]["po_number"] is None, "missing PO stored as empty, not guessed")

    e = ip.process_invoice(SAMPLES / "split_invoice_1.pdf")
    check(e["decision"] == db.DECISION_APPROVED, "split 1 (60,000 of 100,000) APPROVED")
    f = ip.process_invoice(SAMPLES / "split_invoice_2.pdf")
    check(f["decision"] == db.DECISION_APPROVED, "split 2 (cumulative 100,000 = PO amount) APPROVED")
    g = ip.process_invoice(SAMPLES / "split_invoice_3.pdf")
    check(g["decision"] == db.DECISION_REJECTED and "150,000.00" in g["explanation"],
          "split 3 (cumulative 150,000 > limit 105,000) REJECTED, explanation shows cumulative amount")
    check(db.get_po_consumed("PO-2026-1003") == 100000.0, "only the two approved splits consume PO-2026-1003")

    rows = db.get_all_invoices()
    check(len(rows) == 7, "all seven processing attempts are stored (audit trail)")
    check(all(r["validation_results"] for r in rows), "structured validation results stored for every invoice")
    check(rows[-1]["due_date"] == "2026-11-11" and rows[-1]["invoice_date"] == "2026-10-12",
          "invoice and due dates saved in ISO format")
    check(len(db.get_audit_events(a["processing_id"])) == 1, "each save writes an audit event")


def test_vendor_rules():
    section("3. Vendor rules")
    fresh_db()
    ev, _ = submit(fields(vendor_name="Apex Stationers & Traders", po_number="PO-2026-1007",
                          invoice_number="INV-U-1"))
    check(ev["decision"] == db.DECISION_REJECTED and "not approved" in ev["explanation"],
          "UNAPPROVED vendor (Apex Stationers) is REJECTED")
    ev, _ = submit(fields(vendor_name="Totally Unknown Traders Ltd", invoice_number="INV-U-2"))
    check(ev["decision"] == db.DECISION_REJECTED, "vendor not in vendor master is REJECTED")
    ev, _ = submit(fields(vendor_name=None, invoice_number="INV-U-3"))
    check(ev["decision"] == db.DECISION_FLAGGED, "vendor name not extracted -> FLAGGED (uncertain, not rejected)")
    ev, _ = submit(fields(invoice_number="INV-U-4",
                          extraction_notes=["Vendor name conflict: header says 'A' but signature says 'B'."]))
    check(ev["decision"] == db.DECISION_FLAGGED, "conflicting vendor names (header vs signature) -> FLAGGED")
    ev, _ = submit(fields(vendor_name="sharma  OFFICE supplies pvt ltd", invoice_number="INV-U-5"))
    check(ev["decision"] == db.DECISION_APPROVED, "vendor match ignores capitals and extra spaces")


def test_po_rules():
    section("4. Purchase-order rules")
    fresh_db()
    ev, _ = submit(fields(vendor_name="Vikram Industrial Equipment Pvt Ltd", po_number="PO-2026-1008",
                          invoice_number="INV-P-1"))
    check(ev["decision"] == db.DECISION_REJECTED and "inactive" in ev["explanation"], "INACTIVE PO is REJECTED")
    ev, _ = submit(fields(po_number="PO-2026-1002", invoice_number="INV-P-2"))
    check(ev["decision"] == db.DECISION_REJECTED and "do not match" in ev["explanation"],
          "PO belonging to a different vendor is REJECTED (vendor mismatch)")
    ev, _ = submit(fields(po_number=None, invoice_number="INV-P-3"))
    check(ev["decision"] == db.DECISION_FLAGGED, "missing PO number -> FLAGGED")
    ev, _ = submit(fields(po_number="PO-2026-9999", invoice_number="INV-P-4"))
    check(ev["decision"] == db.DECISION_FLAGGED and "not found" in ev["explanation"],
          "PO number not in PO file -> FLAGGED (cannot be validated)")
    ev, _ = submit(fields(currency="USD", invoice_number="INV-P-5"))
    check(ev["decision"] == db.DECISION_FLAGGED, "invoice currency different from PO currency -> FLAGGED")
    check(db.get_po_consumed("PO-2026-1001") == 0.0, "none of the above consumed any PO balance")


def test_missing_or_uncertain_data():
    section("5. Missing / uncertain data is flagged, never approved")
    fresh_db()
    for label, f in [
        ("missing invoice number", fields(invoice_number=None)),
        ("missing invoice date", fields(invoice_date=None, invoice_number="INV-M-2")),
        ("missing total", fields(total_amount=None, invoice_number="INV-M-3")),
        ("missing subtotal", fields(subtotal=None, invoice_number="INV-M-5")),
        ("subtotal + tax != total", fields(total_amount=Decimal("15000.00"), invoice_number="INV-M-6")),
        ("scanned PDF (no text)", fields(vendor_name=None, invoice_number=None, total_amount=None,
                                         extraction_error="No selectable text found; scanned PDF requires OCR.")),
    ]:
        ev, _ = submit(f)
        check(ev["decision"] == db.DECISION_FLAGGED, f"{label} -> FLAGGED")
    ev, _ = submit(fields(invoice_number="INV-M-7", total_amount=Decimal("11800.60")))
    check(ev["decision"] == db.DECISION_APPROVED, "rounding difference under ₹1 is tolerated")


def test_tolerance_and_cumulative():
    section("6. Tolerance band and cumulative PO tracking (PO-2026-1001: 85,000 +5% = 89,250)")
    fresh_db()
    big = lambda n, total: fields(invoice_number=n, subtotal=total / Decimal("1.18"),
                                  tax_amount=total - (total / Decimal("1.18")).quantize(Decimal("0.01")),
                                  total_amount=total)
    ev, _ = submit(big("INV-C-1", Decimal("87000.00")))
    check(ev["decision"] == db.DECISION_FLAGGED and "within tolerance" in ev["explanation"],
          "87,000 is above the PO amount but within tolerance -> FLAGGED")
    check(db.get_po_consumed("PO-2026-1001") == 0.0, "FLAGGED invoice does not consume PO balance")

    ev, _ = submit(big("INV-C-2", Decimal("85000.00")))
    check(ev["decision"] == db.DECISION_APPROVED, "exactly the PO amount (85,000) -> APPROVED")
    check(db.get_po_consumed("PO-2026-1001") == 85000.0, "approved 85,000 consumed")

    ev, _ = submit(big("INV-C-3", Decimal("4000.00")))
    check(ev["decision"] == db.DECISION_FLAGGED, "next 4,000 (cumulative 89,000) is within tolerance -> FLAGGED")
    ev, _ = submit(big("INV-C-4", Decimal("5000.00")))
    check(ev["decision"] == db.DECISION_REJECTED, "next 5,000 (cumulative 90,000 > 89,250) -> REJECTED")
    check(db.get_po_consumed("PO-2026-1001") == 85000.0, "flagged + rejected invoices never consumed anything")

    ev, _ = submit(big("INV-C-5", Decimal("85000.00")))     # same PO, fresh number, PO already full
    check(ev["decision"] == db.DECISION_REJECTED, "a second full-PO invoice is REJECTED (cumulative 170,000)")


def test_duplicate_rules():
    section("7. Duplicate detection")
    fresh_db()
    _, first = submit(fields(invoice_number="INV-D-1"))
    ev, second = submit(fields(invoice_number="  inv-d-1 "))
    check(ev["decision"] == db.DECISION_REJECTED and ev["duplicate"]["processing_id"] == first,
          "same number with different capitals/spaces is caught as a duplicate")
    ev, _ = submit(fields(invoice_number="INV-D-1"))
    check(ev["duplicate"]["processing_id"] == first, "a third submission still points to the FIRST record")
    ev, _ = submit(fields(vendor_name="Bharat IT Solutions Pvt Ltd", po_number="PO-2026-1002",
                          invoice_number="INV-D-1"))
    check(ev["decision"] == db.DECISION_APPROVED, "same invoice number from a DIFFERENT vendor is not a duplicate")
    check(db.get_po_consumed("PO-2026-1001") == 11800.0, "duplicates never consumed PO balance a second time")


def test_real_database_untouched(before):
    section("8. Safety")
    check(sha(REAL_DB) == before, "real invoice_history.db is byte-for-byte unchanged by these tests")


def main():
    global passed, failed
    before = sha(REAL_DB)
    with tempfile.TemporaryDirectory() as folder:
        db.DB_PATH = Path(folder) / "stage4_test.db"
        try:
            db.init_db()
            test_extraction_all_seven_layouts()
            test_end_to_end_sample_pdfs()
            test_vendor_rules()
            test_po_rules()
            test_missing_or_uncertain_data()
            test_tolerance_and_cumulative()
            test_duplicate_rules()
        finally:
            db.DB_PATH = REAL_DB
    test_real_database_untouched(before)
    print(f"\n{passed} passed, {failed} failed.")
    if failed:
        raise SystemExit(1)
    print("All Stage 4 tests passed.")


if __name__ == "__main__":
    main()