"""
test_database.py  -  checks database.py (InvoiceIQ, Stage 3)

Run:   python test_database.py

The tests use a throw-away database in a temporary folder, so your real
invoice_history.db is never touched.
"""

import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import database as db

passed = 0
failed = 0


def check(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def section(title):
    print(f"\n{title}")


def expect_error(error_type, function, *args, **kwargs):
    """True if calling function(...) raises error_type."""
    try:
        function(*args, **kwargs)
    except error_type:
        return True
    except Exception:
        return False
    return False


def make_record(**changes):
    """A valid APPROVED record for PO-2026-1001; override any field you like."""
    record = {
        "filename": "normal_invoice.pdf",
        "vendor_id": "V001",
        "vendor_name": "Sharma Office Supplies Pvt Ltd",
        "invoice_number": "INV-SS-2026-001",
        "invoice_date": "2026-10-12",
        "due_date": "2026-11-11",
        "po_number": "PO-2026-1001",
        "currency": "INR",
        "subtotal": 36000,
        "tax_amount": 6480,
        "total_amount": 42480,
        "decision": db.DECISION_APPROVED,
        "decision_explanation": "All checks passed.",
        "validation_results": [
            {"check": "Vendor check", "result": "PASS", "explanation": "Vendor found in vendor master"},
            {"check": "PO check", "result": "PASS", "explanation": "PO exists and belongs to the vendor"},
        ],
    }
    record.update(changes)
    return record


def run_tests():
    # ------------------------------------------------------------------
    section("1. Database initialisation")
    check(not db.DB_PATH.exists(), "database file does not exist before init_db()")
    db.init_db()
    check(db.DB_PATH.exists(), "init_db() creates the database file")
    with sqlite3.connect(str(db.DB_PATH)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    check({"invoices", "audit_log"} <= names, "tables 'invoices' and 'audit_log' exist")
    check({"ux_invoices_original", "idx_invoices_po", "idx_invoices_decision"} <= names,
          "unique constraint index and lookup indexes exist")

    # ------------------------------------------------------------------
    section("2. Saving an approved invoice")
    before = db.get_po_balance("PO-2026-1001", 85000, 5)
    check(before["consumed"] == 0 and before["remaining_balance"] == 85000.0,
          "PO-2026-1001 starts with nothing consumed (remaining 85,000.00)")
    first_id = db.save_processing_record(make_record())
    check(first_id == 1, "first record gets processing ID 1")
    saved = db.get_invoice(first_id)
    check(saved["decision"] == "APPROVED" and saved["total_amount"] == 42480.0,
          "record can be read back by ID with the right decision and total")
    check(saved["invoice_number"] == "INV-SS-2026-001" and saved["po_number"] == "PO-2026-1001",
          "invoice number and PO number are stored")
    check(len(saved["validation_results"]) == 2 and saved["validation_results"][0]["result"] == "PASS",
          "validation results are stored as structured data")
    check(bool(saved["processed_at"]), "processing timestamp is stored")
    events = db.get_audit_events(first_id)
    check(len(events) == 1 and events[0]["event_type"] == "PROCESSED",
          "an audit event 'PROCESSED' is written automatically")
    db.init_db()
    check(len(db.get_all_invoices()) == 1, "running init_db() again keeps existing data")
    check(db.get_invoice(9999) is None, "unknown processing ID returns None")

    # ------------------------------------------------------------------
    section("3. PO balance after an approval")
    after = db.get_po_balance("PO-2026-1001", 85000, 5)
    check(after["consumed"] == 42480.0, "consumed = 42,480.00")
    check(after["remaining_balance"] == 42520.0, "remaining balance = 85,000 - 42,480 = 42,520.00")
    check(after["max_allowed_cumulative"] == 89250.0, "max allowed with 5% tolerance = 89,250.00")
    check(after["remaining_with_tolerance"] == 46770.0, "remaining with tolerance = 46,770.00")
    check(db.calculate_max_allowed(100000, 5) == 105000.0, "calculate_max_allowed(1,00,000, 5%) = 1,05,000.00")
    check(db.calculate_max_allowed(75000, 2) == 76500.0, "calculate_max_allowed(75,000, 2%) = 76,500.00")
    check(db.calculate_max_allowed(333.33, 7.5) == 358.33, "tolerance maths rounds to 2 decimals")

    # ------------------------------------------------------------------
    section("4. Duplicate detection")
    found = db.find_duplicate("V001", "Sharma Office Supplies Pvt Ltd", "INV-SS-2026-001")
    check(found is not None and found["processing_id"] == first_id,
          "same vendor + invoice number returns the previous record")
    check(db.find_duplicate("V001", None, "  inv-ss-2026-001 ") is not None,
          "capital letters and surrounding spaces do not hide a duplicate")
    check(db.find_duplicate("V001", None, "INV-SS- 2026-001") is not None,
          "spaces inside the number do not hide a duplicate")
    check(db.find_duplicate("V002", "Bharat IT Solutions Pvt Ltd", "INV-SS-2026-001") is None,
          "same number from a DIFFERENT vendor is not a duplicate")
    check(db.find_duplicate("V001", None, "INV-SS-2026-002") is None, "a new invoice number is not a duplicate")
    check(db.find_duplicate("V001", None, None) is None and db.find_duplicate("V001", None, "   ") is None,
          "missing invoice number returns None (processor must flag it, not assume unique)")
    check(db.find_duplicate("V001", None, "INV-SS-2026-001", exclude_processing_id=first_id) is None,
          "a record can be excluded so it does not match itself")

    dup_id = db.save_processing_record(make_record(
        filename="duplicate_invoice.pdf", decision=db.DECISION_REJECTED,
        decision_explanation="Duplicate of processing record #1.", duplicate_of=first_id))
    check(db.get_invoice(dup_id)["duplicate_of"] == first_id, "duplicate record points to the original")
    check(db.get_po_consumed("PO-2026-1001") == 42480.0, "the duplicate does NOT consume PO balance again")
    check(db.find_duplicate("V001", None, "INV-SS-2026-001")["processing_id"] == first_id,
          "a third submission still points to the ORIGINAL, not to the duplicate")
    check(expect_error(db.DuplicateInvoiceError, db.save_processing_record, make_record(filename="again.pdf")),
          "approving the same vendor + invoice number a second time is blocked")
    check(len(db.get_all_invoices()) == 2 and db.get_po_consumed("PO-2026-1001") == 42480.0,
          "the blocked save left no trace (rolled back) and PO balance is unchanged")
    check(expect_error(ValueError, db.save_processing_record,
                       make_record(invoice_number="INV-X", duplicate_of=first_id)),
          "a duplicate can never be saved as APPROVED")

    # ------------------------------------------------------------------
    section("5. FLAGGED and REJECTED invoices do not consume the PO")
    db.save_processing_record(make_record(
        filename="po_exceeded_invoice.pdf", vendor_id="V004", vendor_name="Deccan Facility Services LLP",
        invoice_number="INV-DFS-2026-001", po_number="PO-2026-1004", subtotal=80000, tax_amount=14400,
        total_amount=94400, decision=db.DECISION_REJECTED, decision_explanation="Exceeds the PO limit."))
    db.save_processing_record(make_record(
        filename="flag.pdf", vendor_id="V004", vendor_name="Deccan Facility Services LLP",
        invoice_number="INV-DFS-2026-002", po_number="PO-2026-1004", total_amount=50000,
        decision=db.DECISION_FLAGGED, decision_explanation="Needs a person to look at it."))
    bal = db.get_po_balance("PO-2026-1004", 75000, 2)
    check(bal["consumed"] == 0.0 and bal["remaining_balance"] == 75000.0,
          "PO-2026-1004 still has all 75,000.00 after one REJECTED and one FLAGGED invoice")
    check(bal["approved_invoice_count"] == 0, "no approved invoices are listed against PO-2026-1004")

    # ------------------------------------------------------------------
    section("6. Split invoices on one PO (PO-2026-1003: 1,00,000 + 5%)")
    for number, amount in [("SPL-KLF-001", 60000), ("SPL-KLF-002", 40000)]:
        db.save_processing_record(make_record(
            filename=f"{number}.pdf", vendor_id="V003", vendor_name="Kaveri Logistics & Freight Pvt Ltd",
            invoice_number=number, po_number="PO-2026-1003", subtotal=None, tax_amount=None,
            total_amount=amount))
        if amount == 60000:
            b = db.get_po_balance("PO-2026-1003", 100000, 5)
            check(b["consumed"] == 60000.0 and b["remaining_balance"] == 40000.0,
                  "after invoice 1: consumed 60,000.00, remaining 40,000.00")
    b = db.get_po_balance("PO-2026-1003", 100000, 5)
    check(b["consumed"] == 100000.0 and b["remaining_balance"] == 0.0,
          "after invoice 2: consumed 1,00,000.00, remaining 0.00")
    check(b["max_allowed_cumulative"] == 105000.0 and b["remaining_with_tolerance"] == 5000.0,
          "only 5,000.00 of tolerance headroom is left")
    check(50000 > b["remaining_with_tolerance"] and round(50000 - b["remaining_with_tolerance"], 2) == 45000.0,
          "invoice 3 (50,000.00) would exceed the limit by 45,000.00")
    db.save_processing_record(make_record(
        filename="split_invoice_3.pdf", vendor_id="V003", vendor_name="Kaveri Logistics & Freight Pvt Ltd",
        invoice_number="SPL-KLF-003", po_number="PO-2026-1003", total_amount=50000,
        decision=db.DECISION_REJECTED, decision_explanation="Exceeds remaining PO balance."))
    check(db.get_po_consumed("PO-2026-1003") == 100000.0, "rejected invoice 3 leaves the consumed amount at 1,00,000.00")
    check([i["invoice_number"] for i in db.get_po_invoices("PO-2026-1003")] == ["SPL-KLF-001", "SPL-KLF-002"],
          "get_po_invoices lists exactly the two approved invoices")
    check(db.get_po_consumed("po-2026-1003 ") == 100000.0, "PO numbers are matched ignoring case and spaces")

    # ------------------------------------------------------------------
    section("7. Two-decimal accuracy")
    for n in range(3):
        db.save_processing_record(make_record(
            filename=f"acc{n}.pdf", invoice_number=f"ACC-{n}", po_number="PO-TEST-ACC", total_amount=33333.33))
    check(db.get_po_consumed("PO-TEST-ACC") == 99999.99, "3 x 33,333.33 = exactly 99,999.99")
    for n, amount in enumerate([0.10, 0.20]):
        db.save_processing_record(make_record(
            filename=f"pennies{n}.pdf", invoice_number=f"PEN-{n}", po_number="PO-TEST-PENNIES", total_amount=amount))
    check(0.1 + 0.2 != 0.3 and db.get_po_consumed("PO-TEST-PENNIES") == 0.3,
          "0.10 + 0.20 = exactly 0.30 (plain float maths would give 0.30000000000000004)")
    check(db.to_money("1,18,000.456") == db.to_money("118000.46"), "text amounts with commas are understood")
    check(expect_error(ValueError, db.to_money, "abc"), "rubbish amounts raise a clear error")

    # ------------------------------------------------------------------
    section("8. Human review: reprocessing a FLAGGED invoice")
    flagged_id = db.save_processing_record(make_record(
        filename="missing_po_invoice.pdf", vendor_id="V010", vendor_name="Orchid Catering Services",
        invoice_number="INV-OCS-2026-001", po_number=None, subtotal=20000, tax_amount=3600,
        total_amount=23600, decision=db.DECISION_FLAGGED, decision_explanation="PO number missing."))
    check(db.get_po_consumed("PO-2026-1011") == 0.0, "flagged invoice has not consumed PO-2026-1011")
    db.update_processing_record(
        flagged_id,
        make_record(filename="missing_po_invoice.pdf", vendor_id="V010", vendor_name="Orchid Catering Services",
                    invoice_number="INV-OCS-2026-001", po_number="PO-2026-1011", subtotal=20000,
                    tax_amount=3600, total_amount=23600, decision_explanation="Passed after PO was corrected."),
        corrections={"po_number": {"old": None, "new": "PO-2026-1011"}})
    check(db.get_invoice(flagged_id)["decision"] == "APPROVED", "corrected invoice is now APPROVED")
    check(db.get_po_consumed("PO-2026-1011") == 23600.0, "approval after review consumes the PO exactly once")
    events = db.get_audit_events(flagged_id)
    check([e["event_type"] for e in events] == ["PROCESSED", "MANUAL_CORRECTION"],
          "audit trail shows PROCESSED then MANUAL_CORRECTION")
    check(events[1]["details"]["corrections"]["po_number"]["new"] == "PO-2026-1011",
          "the correction (old and new value) is recorded")
    check(expect_error(ValueError, db.update_processing_record, flagged_id, make_record()),
          "an APPROVED record can no longer be edited")
    check(expect_error(ValueError, db.update_processing_record, 99999, make_record()),
          "updating an unknown record raises an error")

    # ------------------------------------------------------------------
    section("9. Missing invoice numbers")
    a = db.save_processing_record(make_record(filename="nonum1.pdf", invoice_number=None,
                                              decision=db.DECISION_FLAGGED, decision_explanation="No invoice number."))
    b = db.save_processing_record(make_record(filename="nonum2.pdf", invoice_number="",
                                              decision=db.DECISION_FLAGGED, decision_explanation="No invoice number."))
    check(a != b, "two invoices without a number can both be stored (never treated as the same invoice)")
    check(db.get_invoice(a)["invoice_number"] is None, "a missing invoice number is stored as empty, not as text")

    # ------------------------------------------------------------------
    section("10. Reading history and filters")
    all_rows = db.get_all_invoices()
    check(len(all_rows) == len(set(r["processing_id"] for r in all_rows)), "get_all_invoices returns every record once")
    check(all(r["decision"] == "REJECTED" for r in db.get_invoices(decision=db.DECISION_REJECTED))
          and len(db.get_invoices(decision=db.DECISION_REJECTED)) == 3,
          "filter by decision REJECTED returns the 3 rejected records")
    check(all(r["vendor_name"] == "Kaveri Logistics & Freight Pvt Ltd"
              for r in db.get_invoices(vendor_name="Kaveri Logistics & Freight Pvt Ltd"))
          and len(db.get_invoices(vendor_name="Kaveri Logistics & Freight Pvt Ltd")) == 3,
          "filter by vendor returns only that vendor (3 records)")
    tomorrow = date.today() + timedelta(days=1)
    check(db.get_invoices(date_from=tomorrow) == [] and len(db.get_invoices(date_to=tomorrow)) == len(all_rows),
          "date filters work (nothing from tomorrow onwards)")
    check(len(db.get_invoices(limit=2)) == 2, "limit works")
    check("Orchid Catering Services" in db.get_vendor_names(), "get_vendor_names lists vendors in the history")
    check(expect_error(ValueError, db.get_invoices, decision="MAYBE"), "an invalid decision filter raises an error")
    evil = db.get_invoices(vendor_name="'; DROP TABLE invoices; --")
    check(evil == [] and len(db.get_all_invoices()) == len(all_rows),
          "SQL injection attempt finds nothing and the table is intact")

    # ------------------------------------------------------------------
    section("11. Dashboard numbers")
    stats = db.get_dashboard_stats()
    rows = db.get_all_invoices()
    check(stats["total_invoices"] == len(rows), "total invoices matches the stored records")
    check(stats["approved"] + stats["flagged"] + stats["rejected"] == stats["total_invoices"],
          "approved + flagged + rejected = total")
    check(stats["approved"] == sum(r["decision"] == "APPROVED" for r in rows), "approved count is correct")
    check(stats["duplicates_detected"] == 1, "one duplicate submission was detected")
    check(stats["approval_rate"] == round(stats["approved"] / stats["total_invoices"] * 100, 1),
          "approval rate is approved / total")
    expected_value = round(sum(r["total_amount"] or 0 for r in rows if r["duplicate_of"] is None), 2)
    check(stats["total_value_processed"] == expected_value, "total value excludes duplicate submissions")
    check(sum(t["count"] for t in db.get_processing_trend()) == len(rows), "trend rows add up to every record")
    vendors = db.get_amounts_by_vendor()
    check(sum(v["invoices"] for v in vendors) == len(rows) - 1, "vendor totals cover every non-duplicate record")

    # ------------------------------------------------------------------
    section("12. Bad input is rejected before it reaches the database")
    check(expect_error(ValueError, db.save_processing_record, make_record(decision="MAYBE")), "unknown decision")
    check(expect_error(ValueError, db.save_processing_record, make_record(decision_explanation="  ")),
          "empty explanation")
    check(expect_error(ValueError, db.save_processing_record, make_record(filename=None)), "missing filename")
    check(expect_error(ValueError, db.save_processing_record, make_record(invoice_number="Z-1", total_amount=None)),
          "APPROVED invoice without a total")

    # ------------------------------------------------------------------
    section("13. Reset (used by the Test Scenarios page)")
    db.reset_database()
    check(db.get_all_invoices() == [] and db.get_dashboard_stats()["total_invoices"] == 0,
          "reset_database() empties the history")
    check(db.get_dashboard_stats()["approval_rate"] == 0.0, "dashboard handles an empty database (no divide by zero)")
    new_id = db.save_processing_record(make_record())
    check(new_id == 1, "after a reset the IDs start again from 1 and the invoice can be processed again")


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        real_path = db.DB_PATH
        db.DB_PATH = Path(folder) / "test_invoice_history.db"      # use a throw-away database
        try:
            run_tests()
        finally:
            db.DB_PATH = real_path
    print(f"\n{passed} passed, {failed} failed.")
    if failed:
        sys.exit(1)
    print("All database tests passed.")


if __name__ == "__main__":
    main()