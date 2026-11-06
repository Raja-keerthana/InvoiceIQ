"""
test_app.py  -  checks the Stage 5 Streamlit app (app.py + ui_helpers.py)

Run:   python test_app.py

Uses Streamlit's built-in AppTest and a throw-away database in a temporary
folder.  Your real invoice_history.db is never written to (checked at the end).
"""
import hashlib
import tempfile
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

import database as db
import ui_helpers as ui

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "sample_invoices"
REAL_DB = db.DB_PATH
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


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def open_page(page, **state):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["page"] = page
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def no_errors(at):
    return len(at.exception) == 0 and len(at.error) == 0


def button(at, key):
    return next((b for b in at.button if b.key == key), None)


def metric_values(at):
    return {m.label: m.value for m in at.metric}


# --------------------------------------------------------------------------
def test_pipeline_function():
    section("1. Backend glue function process_pdf_bytes (same backend as Stage 4)")
    db.reset_database()
    order = [("normal_invoice.pdf", db.DECISION_APPROVED), ("duplicate_invoice.pdf", db.DECISION_REJECTED),
             ("po_exceeded_invoice.pdf", db.DECISION_REJECTED), ("missing_po_invoice.pdf", db.DECISION_FLAGGED),
             ("split_invoice_1.pdf", db.DECISION_APPROVED), ("split_invoice_2.pdf", db.DECISION_APPROVED),
             ("split_invoice_3.pdf", db.DECISION_REJECTED)]
    events = []
    for name, expected in order:
        result = ui.process_pdf_bytes(name, (SAMPLES / name).read_bytes(),
                                      on_event=lambda e, p: events.append(e) if name == "normal_invoice.pdf" else None)
        check(result["decision"] == expected, f"{name} -> {expected}")
    check(events == ["extract_start", "extract_done", "evaluated", "saving", "saved"],
          "progress events fire in order: extract, evaluate, save")
    check(db.get_po_consumed("PO-2026-1003") == 100000.0, "split invoices consume exactly 100,000 of PO-2026-1003")
    check(len(db.get_all_invoices()) == 7, "seven records saved")

    before = len(db.get_all_invoices())
    for label, data, expected_type in [("not a PDF", b"hello world", ValueError), ("empty file", b"", ValueError),
                                        ("corrupt PDF", b"%PDF-1.4 this is not really a pdf", Exception)]:
        try:
            ui.process_pdf_bytes("bad.pdf", data)
            raised = None
        except Exception as error:
            raised = error
        check(isinstance(raised, expected_type), f"{label} raises an error instead of saving garbage")
    check(len(db.get_all_invoices()) == before, "failed uploads add no records")

    # Check only folders created by this call. Other InvoiceIQ temp folders may
    # legitimately exist in the shared OS temp directory (for example, from an
    # interrupted earlier run), so a global "no matching folders" assertion is
    # too broad and can fail even when this function cleans up correctly.
    temp_root = Path(tempfile.gettempdir())
    temp_dirs_before = set(temp_root.glob("invoiceiq_*"))
    result = ui.process_pdf_bytes("..\\..\\evil:name?.pdf", (SAMPLES / "missing_po_invoice.pdf").read_bytes())
    temp_dirs_after = set(temp_root.glob("invoiceiq_*"))
    check(result["filename"] == "evil:name?.pdf", "path parts stripped from the uploaded file name")
    check(not (temp_dirs_after - temp_dirs_before), "temporary upload folders created by this call are cleaned up")


def test_helpers():
    section("2. Display helpers")
    check("<script>" not in ui.esc("<script>alert(1)</script>"), "HTML from a PDF is escaped")
    check(ui.money(42480) == "₹42,480.00" and ui.money(None) == "—" and ui.money("36000.00") == "₹36,000.00",
          "money() formats rupees, blanks and strings")
    check(ui.worst_status(["PASS", "REVIEW", "FAIL"]) == "FAIL" and ui.worst_status(["PASS", "SKIPPED"]) == "PASS",
          "worst_status picks the most serious result")
    grouped = ui.group_checks_by_stage([{"check": "Vendor validation", "result": "PASS"},
                                        {"check": "PO amount validation", "result": "SKIPPED"},
                                        {"check": "Something new", "result": "PASS"}])
    check(len(grouped["vendor"]) == 1 and len(grouped["financial"]) == 2, "checks grouped into the six stages")


def test_empty_database_pages():
    section("3. Every page opens with an EMPTY database")
    db.reset_database()
    for page in ["Dashboard", "Process Invoice", "Processing Details", "Invoice History", "Test Scenarios"]:
        at = open_page(page)
        check(no_errors(at), f"{page}: no errors")
    at = open_page("Dashboard")
    check(metric_values(at).get("Total invoices processed") == "0", "dashboard shows 0 invoices")
    at = open_page("Process Invoice")
    process_button = next(b for b in at.button if b.label == "Process Invoice")
    check(process_button.disabled, "Process Invoice button disabled until a PDF is chosen")
    at = open_page("Test Scenarios")
    check(len(at.button) >= 8 and len(db.get_all_invoices()) == 0,
          "Test Scenarios lists the samples but does NOT process anything on start-up")


def test_scenarios_page_flow():
    section("4. Test Scenarios page: real button clicks")
    db.reset_database()
    at = open_page("Test Scenarios")
    run_all = next(b for b in at.button if b.label == "Run all 7 samples")
    check(run_all.disabled, "'Run all' is locked until the confirmation box is ticked")

    at = open_page("Test Scenarios")
    button(at, "run_normal_invoice.pdf").click().run()
    check(no_errors(at) and [r["decision"] for r in db.get_all_invoices()] == [db.DECISION_APPROVED],
          "clicking 'Process this sample' on the normal invoice saves one APPROVED record")
    check(any("✔ matches" in m.value for m in at.markdown), "page shows Actual vs Expected: matches")

    button(at, "run_duplicate_invoice.pdf").click().run()
    newest = db.get_all_invoices()[0]
    check(newest["decision"] == db.DECISION_REJECTED and newest["duplicate_of"] == 1,
          "duplicate sample is REJECTED and points to record #1")

    button(at, "run_normal_invoice.pdf").click().run()
    check(db.get_all_invoices()[0]["decision"] == db.DECISION_REJECTED,
          "processing the normal sample a 2nd time is rejected as a duplicate")
    check(any("✖ differs" in m.value for m in at.markdown), "page flags the mismatch with the expected outcome")

    db.reset_database()
    at = open_page("Test Scenarios")
    at.checkbox(key="confirm_run_all").check().run()
    next(b for b in at.button if b.label == "Run all 7 samples").click().run()
    decisions = [r["decision"] for r in reversed(db.get_all_invoices())]
    check(decisions == [db.DECISION_APPROVED, db.DECISION_REJECTED, db.DECISION_REJECTED, db.DECISION_FLAGGED,
                        db.DECISION_APPROVED, db.DECISION_APPROVED, db.DECISION_REJECTED],
          "'Run all 7' gives the expected result for every scenario")
    check(no_errors(at), "no errors after running all seven")


def test_pages_with_data():
    section("5. Pages with data (after the seven samples)")
    at = open_page("Dashboard")
    m = metric_values(at)
    check(no_errors(at), "Dashboard: no errors")
    check(m["Total invoices processed"] == "7" and m["Approved"] == "3" and m["Flagged for review"] == "1"
          and m["Rejected"] == "3", "Dashboard counts: 7 total / 3 approved / 1 flagged / 3 rejected")
    check(m["Duplicate invoices"] == "1", "Dashboard: 1 duplicate detected")
    check(m["Approval rate"] == "42.9%", "Dashboard: approval rate 42.9% (3 of 7)")
    stats = db.get_dashboard_stats()
    check(m["Total invoice amount"] == ui.money(stats["total_value_processed"]),
          "Dashboard total amount equals the database figure (duplicates excluded)")

    at = open_page("Invoice History")
    check(no_errors(at), "Invoice History: no errors")
    at = open_page("Invoice History")
    at.text_input[0].set_value("kaveri").run()
    check(no_errors(at), "history search box works without errors")
    at = open_page("Invoice History")
    at.multiselect[0].set_value([db.DECISION_REJECTED]).run()
    check(no_errors(at), "history decision filter works without errors")

    newest = db.get_all_invoices()[0]
    at = open_page("Processing Details", details_pid=newest["processing_id"])
    check(no_errors(at), "Processing Details: no errors for a record")
    html = " ".join(m.value for m in at.markdown)
    check("REJECTED" in html or any(newest["decision"] in str(e.value) for e in at.markdown),
          "details page shows the decision and checks")
    check(any(c in html for c in ("FAIL", "PASS")), "details page shows PASS / FAIL statuses")

    dup = next(r for r in db.get_all_invoices() if r["duplicate_of"])
    at = open_page("Processing Details", details_pid=dup["processing_id"])
    check(no_errors(at) and any("Open original record" in b.label for b in at.button),
          "duplicate record offers a link to its original")

    last = ui.process_pdf_bytes("missing_po_invoice.pdf", (SAMPLES / "missing_po_invoice.pdf").read_bytes())
    at = open_page("Process Invoice", last_result=last)
    check(no_errors(at), "Process Invoice page renders a stored result without errors")
    check(len(at.tabs) == 5, "result shows five tabs (checks, fields, reasoning, PO balance, text)")


class FakeUpload:
    """Stands in for Streamlit's UploadedFile (AppTest cannot drive the real uploader)."""
    def __init__(self, name, data):
        self.name, self._data, self.size = name, data, len(data)

    def getvalue(self):
        return self._data


def test_process_page_live_run():
    section("5b. Process Invoice page: upload, click, live stages")
    db.reset_database()
    real_uploader = st.file_uploader
    holder = {"file": FakeUpload("normal_invoice.pdf", (SAMPLES / "normal_invoice.pdf").read_bytes())}
    st.file_uploader = lambda *a, **k: holder["file"]
    try:
        at = open_page("Process Invoice")
        check(any("normal_invoice.pdf" in m.value for m in at.markdown), "uploaded file name is displayed")
        process_button = next(b for b in at.button if b.label == "Process Invoice")
        check(not process_button.disabled, "Process Invoice button enabled once a PDF is chosen")
        check(len(db.get_all_invoices()) == 0, "nothing is processed until the button is pressed")

        process_button.click().run()
        check(no_errors(at), "clicking Process Invoice runs without errors")
        rows = db.get_all_invoices()
        check(len(rows) == 1 and rows[0]["decision"] == db.DECISION_APPROVED, "invoice processed and saved as APPROVED")
        stage_rows = [m.value for m in at.markdown if 'class="iq-stage"' in m.value]
        check(len(stage_rows) == 6, "all six processing stages are displayed")
        for name in ["PDF text extraction", "Vendor identification", "Purchase order validation",
                     "Duplicate detection", "Financial validation", "Final decision"]:
            check(any(name in r for r in stage_rows), f"stage shown: {name}")
        check("Approved" in stage_rows[-1], "final stage shows the decision")
        check(any("APPROVED" in m.value and "iq-banner" in m.value for m in at.markdown),
              "final decision is shown prominently (banner)")
        check(len(at.tabs) == 5, "result tabs shown (checks, fields, reasoning, PO balance, text)")

        # same file again -> duplicate, shown as REJECTED
        at = open_page("Process Invoice")
        check(no_errors(at), "page redraws cleanly after a run")
        next(b for b in at.button if b.label == "Process Invoice").click().run()
        rows = db.get_all_invoices()
        check(len(rows) == 2 and rows[0]["decision"] == db.DECISION_REJECTED and rows[0]["duplicate_of"] == 1,
              "uploading the same invoice again is REJECTED as a duplicate of #1")
        check(any("REJECTED" in m.value and "iq-banner" in m.value for m in at.markdown), "rejection banner shown")

        # a non-PDF upload: friendly error, nothing saved
        holder["file"] = FakeUpload("notes.pdf", b"just some text")
        at = open_page("Process Invoice")
        next(b for b in at.button if b.label == "Process Invoice").click().run()
        check(len(at.exception) == 0 and any("PDF" in e.value for e in at.error),
              "non-PDF upload shows a friendly error (no crash)")
        check(len(db.get_all_invoices()) == 2, "failed upload saved nothing")

        # re-draw without clicking: latest result stays visible
        holder["file"] = None
        at = open_page("Process Invoice", last_result=ui.process_pdf_bytes(
            "missing_po_invoice.pdf", (SAMPLES / "missing_po_invoice.pdf").read_bytes()))
        check(no_errors(at) and any("Latest result" in s.value for s in at.subheader),
              "last result remains visible when the page is redrawn")
    finally:
        st.file_uploader = real_uploader


def test_real_db_untouched(before):
    section("6. Safety")
    check(sha(REAL_DB) == before, "real invoice_history.db is byte-for-byte unchanged by these tests")


def main():
    global passed, failed
    before = sha(REAL_DB)
    with tempfile.TemporaryDirectory() as folder:
        db.DB_PATH = Path(folder) / "ui_test.db"
        try:
            db.init_db()
            assert db.DB_PATH != REAL_DB
            test_pipeline_function()
            test_helpers()
            test_empty_database_pages()
            test_scenarios_page_flow()
            test_pages_with_data()
            test_process_page_live_run()
        finally:
            db.DB_PATH = REAL_DB
    test_real_db_untouched(before)
    print(f"\n{passed} passed, {failed} failed.")
    if failed:
        raise SystemExit(1)
    print("All Stage 5 UI tests passed.")


if __name__ == "__main__":
    main()
