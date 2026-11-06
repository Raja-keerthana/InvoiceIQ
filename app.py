"""
app.py  -  InvoiceIQ, Stage 5: Streamlit application

Run from the InvoiceIQ folder:   streamlit run app.py

The pages only DISPLAY and TRIGGER backend work.  All rules live in
invoice_processor.py and all storage lives in database.py (invoice_history.db).
"""
import json
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import database as db
from ui_helpers import (
    DECISION_COLOR, STAGES, decision_banner, esc, extraction_summary,
    group_checks_by_stage, inject_css, money, process_pdf_bytes, records_to_df, render_checks,
    render_fields, render_reasoning, show_df, show_plot, stage_detail, stage_html,
    with_decision_dot, worst_status,
)

st.set_page_config(page_title="InvoiceIQ", page_icon="🧾", layout="wide")
inject_css()
db.init_db()          # creates the tables only if they are missing - never touches existing data

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "sample_invoices"
PAGES = ["Dashboard", "Process Invoice", "Processing Details", "Invoice History", "Test Scenarios"]
DECISION_TO_STATUS = {db.DECISION_APPROVED: "PASS", db.DECISION_FLAGGED: "REVIEW", db.DECISION_REJECTED: "FAIL"}
DECISION_WORD = {db.DECISION_APPROVED: "Approved", db.DECISION_FLAGGED: "Flagged", db.DECISION_REJECTED: "Rejected"}


# ==========================================================================
# Navigation helpers
# ==========================================================================
def go_to(page, processing_id=None):
    """Button callback: jump to another page (optionally preselecting a record)."""
    st.session_state["page"] = page
    if processing_id is not None:
        st.session_state["details_pid"] = processing_id


# ==========================================================================
# Shared: full details of one stored record
# ==========================================================================
def render_record_details(rec, key_prefix="rec"):
    decision_banner(rec["decision"], rec["decision_explanation"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Processing ID", f"#{rec['processing_id']}")
    c2.metric("Processed at", rec["processed_at"])
    c3.metric("Invoice total", money(rec["total_amount"]))
    c4.metric("PO number", rec["po_number"] or "— (none)")
    st.caption(f"File: **{rec['filename']}**")

    if rec.get("duplicate_of"):
        st.info(f"This submission is a duplicate of processing record #{rec['duplicate_of']}.")
        st.button(f"Open original record #{rec['duplicate_of']}", key=f"{key_prefix}_orig_{rec['processing_id']}",
                  on_click=go_to, args=("Processing Details", rec["duplicate_of"]))

    st.subheader("Why this decision?")
    render_reasoning(rec["validation_results"])

    st.subheader("Validation checks")
    render_checks(rec["validation_results"])

    st.subheader("Extracted invoice information")
    fields = dict(rec.get("extracted_fields") or {})
    if not fields:        # very old records: fall back to the stored columns
        fields = {k: rec.get(k) for k in ("vendor_name", "invoice_number", "invoice_date", "due_date",
                                           "po_number", "currency", "subtotal", "tax_amount", "total_amount")}
    render_fields(fields)

    st.subheader("Audit trail")
    events = db.get_audit_events(rec["processing_id"])
    if events:
        show_df(pd.DataFrame([{"Time": e["event_time"], "Event": e["event_type"],
                               "Details": json.dumps(e["details"], ensure_ascii=False, default=str)}
                              for e in events]), hide_index=True)
    else:
        st.caption("No audit events recorded.")
    with st.expander("Raw stored record (JSON)"):
        st.json(rec, expanded=False)


# ==========================================================================
# PAGE 1 - Dashboard
# ==========================================================================
def page_dashboard():
    st.title("Dashboard")
    st.caption("Live numbers from invoice_history.db")
    stats = db.get_dashboard_stats()

    a = st.columns(4)
    a[0].metric("Total invoices processed", stats["total_invoices"])
    a[1].metric("Approved", stats["approved"])
    a[2].metric("Flagged for review", stats["flagged"])
    a[3].metric("Rejected", stats["rejected"])
    b = st.columns(4)
    b[0].metric("Duplicate invoices", stats["duplicates_detected"])
    b[1].metric("Total invoice amount", money(stats["total_value_processed"]),
                help="Excludes duplicate submissions, so the same invoice is never counted twice.")
    b[2].metric("Approved amount", money(stats["approved_value"]))
    b[3].metric("Approval rate", f"{stats['approval_rate']:.1f}%",
                help="Approved invoices ÷ all processing records.")

    if stats["total_invoices"] == 0:
        st.info("No invoices have been processed yet. Upload one on **Process Invoice**, "
                "or try the sample invoices on **Test Scenarios**.")
        st.button("Go to Process Invoice", on_click=go_to, args=("Process Invoice",))
        return

    st.divider()
    left, right = st.columns([1, 1.6])
    with left:
        st.subheader("Invoice decisions")
        counts = pd.DataFrame({
            "Decision": [db.DECISION_APPROVED, db.DECISION_FLAGGED, db.DECISION_REJECTED],
            "Invoices": [stats["approved"], stats["flagged"], stats["rejected"]],
        })
        counts = counts[counts["Invoices"] > 0]
        fig = px.pie(counts, names="Decision", values="Invoices", hole=0.55, color="Decision",
                     color_discrete_map=DECISION_COLOR)
        fig.update_traces(textinfo="value+percent", sort=False)
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=-0.1))
        show_plot(fig)
    with right:
        st.subheader("Processing trend")
        trend = pd.DataFrame(db.get_processing_trend())
        fig = px.bar(trend, x="day", y="count", color="decision", barmode="stack",
                     color_discrete_map=DECISION_COLOR,
                     category_orders={"decision": [db.DECISION_APPROVED, db.DECISION_FLAGGED, db.DECISION_REJECTED]},
                     labels={"day": "Processing date", "count": "Invoices", "decision": "Decision"})
        fig.update_xaxes(type="category")
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=-0.25))
        show_plot(fig)

    st.subheader("Invoice value by vendor")
    vendors = pd.DataFrame(db.get_amounts_by_vendor())
    if not vendors.empty:
        long = vendors.melt(id_vars="vendor", value_vars=["total_value", "approved_value"],
                            var_name="Measure", value_name="Amount (₹)")
        long["Measure"] = long["Measure"].map({"total_value": "All invoices (excl. duplicates)",
                                               "approved_value": "Approved"})
        fig = px.bar(long, y="vendor", x="Amount (₹)", color="Measure", barmode="group", orientation="h",
                     color_discrete_map={"All invoices (excl. duplicates)": "#94a3b8", "Approved": "#15803d"},
                     labels={"vendor": ""})
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=-0.25),
                          yaxis=dict(autorange="reversed"))
        show_plot(fig)

    st.subheader("Recent invoice activity")
    recent = records_to_df(db.get_invoices(limit=10))
    show_df(with_decision_dot(recent[["ID", "Processed", "File", "Invoice No.", "Vendor", "Amount (₹)", "Decision"]]),
            hide_index=True, column_config={"Amount (₹)": st.column_config.NumberColumn(format="%.2f")})


# ==========================================================================
# PAGE 2 - Process Invoice
# ==========================================================================
def render_static_stages(result):
    """The six stages as they ended, for when the page is redrawn after a run."""
    grouped = group_checks_by_stage(result["validation_results"])
    for key, name in STAGES:
        if key == "decision":
            st.markdown(stage_html(name, "done", DECISION_TO_STATUS[result["decision"]],
                                   result["decision"], DECISION_WORD[result["decision"]]),
                        unsafe_allow_html=True)
            continue
        checks = grouped[key]
        detail = stage_detail(checks)
        if key == "extract":
            detail = (extraction_summary(result["fields"]) + " " + detail).strip()
        st.markdown(stage_html(name, "done", worst_status([c["result"] for c in checks]) if checks else "PASS",
                               detail), unsafe_allow_html=True)


def render_result(result):
    decision_banner(result["decision"], result["explanation"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Processing ID", f"#{result['processing_id']}")
    c2.metric("Invoice total", money(result["record"]["total_amount"]))
    c3.metric("PO number", result["record"]["po_number"] or "— (none)")
    if result["record"].get("duplicate_of"):
        st.info(f"Duplicate of processing record #{result['record']['duplicate_of']}.")

    tabs = st.tabs(["Validation checks", "Extracted fields", "Why this decision?", "PO balance", "Extracted text"])
    with tabs[0]:
        render_checks(result["validation_results"])
    with tabs[1]:
        render_fields(result["fields"])
    with tabs[2]:
        st.markdown(f"**Decision:** {result['decision']}")
        st.write(result["explanation"])
        render_reasoning(result["validation_results"])
    with tabs[3]:
        bal = result.get("po_balance")
        if not bal:
            st.info("No PO balance was calculated for this invoice (no valid active PO, a duplicate, "
                    "or the invoice total was missing).")
        else:
            st.caption("Position of the PO *before* this invoice. Only APPROVED invoices use up PO balance.")
            m = st.columns(4)
            m[0].metric(f"PO {bal['po_number']} amount", money(bal["po_amount"]))
            m[1].metric("Already approved", money(bal["consumed"]))
            m[2].metric(f"Limit incl. {bal['tolerance_percentage']:g}% tolerance", money(bal["max_allowed_cumulative"]))
            m[3].metric("Room left (with tolerance)", money(bal["remaining_with_tolerance"]))
    with tabs[4]:
        raw = result["fields"].get("raw_text") or ""
        if raw:
            st.code(raw[:6000], language=None)
        else:
            st.info("No text could be read from this PDF.")
    st.button("Open in Processing Details", key="open_details_last", on_click=go_to,
              args=("Processing Details", result["processing_id"]))


def run_live_processing(uploaded):
    """Run the backend and repaint the six stages as each one finishes."""
    st.subheader("Processing stages")
    slots = {key: st.empty() for key, _ in STAGES}
    names = dict(STAGES)
    for key, name in STAGES:
        slots[key].markdown(stage_html(name, "pending"), unsafe_allow_html=True)
    ctx = {}

    def paint(key, state, status=None, detail="", word=None):
        slots[key].markdown(stage_html(names[key], state, status, detail, word), unsafe_allow_html=True)

    def on_event(event, payload):
        if event == "extract_start":
            paint("extract", "running", detail="Reading the text of the PDF…")
        elif event == "extract_done":
            ctx["fields"] = payload
        elif event == "evaluated":
            grouped = group_checks_by_stage(payload["validation_results"])
            for key in ("extract", "vendor", "po", "duplicate", "financial"):
                if key != "extract":
                    paint(key, "running", detail="Checking…")
                    time.sleep(0.3)           # visual pacing only; the checks already ran in one backend call
                checks = grouped[key]
                detail = stage_detail(checks)
                if key == "extract":
                    detail = (extraction_summary(ctx["fields"]) + " " + detail).strip()
                paint(key, "done", worst_status([c["result"] for c in checks]) if checks else "PASS", detail)
        elif event == "saving":
            paint("decision", "running", detail="Applying the decision rules and saving to the database…")
        elif event == "saved":
            paint("decision", "done", DECISION_TO_STATUS[payload["decision"]], payload["decision"],
                  DECISION_WORD[payload["decision"]])

    try:
        with st.spinner("Processing invoice…"):
            result = process_pdf_bytes(uploaded.name, uploaded.getvalue(), on_event)
    except ValueError as error:
        st.error(str(error))
        return None
    except Exception as error:     # damaged PDF, locked database, etc. - show it, don't crash the app
        st.error(f"The invoice could not be processed: {error}")
        with st.expander("Technical details"):
            st.exception(error)
        return None

    st.session_state["last_result"] = result
    st.session_state.setdefault("processed_uploads", {})[(uploaded.name, uploaded.size)] = result["processing_id"]
    return result


def page_process():
    st.title("Process Invoice")
    st.caption("Upload a text-based PDF invoice. It is extracted, validated and decided with fixed Python rules "
               "(no AI model makes the decision), then saved to the invoice history.")
    uploaded = st.file_uploader("Invoice PDF", type=["pdf"], key="uploader")
    if uploaded is not None:
        st.markdown(f"**Selected file:** `{uploaded.name}` &nbsp;·&nbsp; {uploaded.size / 1024:,.1f} KB")
        earlier = st.session_state.get("processed_uploads", {}).get((uploaded.name, uploaded.size))
        if earlier:
            st.info(f"This file was already processed in this session (record #{earlier}). "
                    "Processing it again will be rejected as a duplicate invoice.")

    clicked = st.button("Process Invoice", type="primary", disabled=uploaded is None)

    result = None
    if clicked and uploaded is not None:
        st.divider()
        result = run_live_processing(uploaded)
        if result:
            st.divider()
            st.subheader("Result")
            render_result(result)
        return

    last = st.session_state.get("last_result")
    if last:
        st.divider()
        st.subheader(f"Latest result — {last['filename']}")
        with st.expander("Processing stages", expanded=False):
            render_static_stages(last)
        render_result(last)
    elif uploaded is None:
        st.info("Choose a PDF above, then press **Process Invoice**.")


# ==========================================================================
# PAGE 3 - Processing Details
# ==========================================================================
def page_details():
    st.title("Processing Details")
    st.caption("Every validation check, the extracted data and the reasoning behind one decision.")
    records = db.get_invoices()
    if not records:
        st.info("Nothing to show yet - process an invoice first.")
        st.button("Go to Process Invoice", on_click=go_to, args=("Process Invoice",))
        return
    ids = [r["processing_id"] for r in records]
    labels = {r["processing_id"]: f"#{r['processing_id']}  ·  {r['filename']}  ·  {r['decision']}" for r in records}
    if st.session_state.get("details_pid") not in ids:
        st.session_state["details_pid"] = ids[0]          # newest record
    pid = st.selectbox("Record to inspect", ids, format_func=labels.get, key="details_pid")
    rec = db.get_invoice(pid)
    if rec is None:
        st.error("That record no longer exists.")
        return
    render_record_details(rec, key_prefix="details")


# ==========================================================================
# PAGE 4 - Invoice History
# ==========================================================================
def history_table(df):
    """Show the table with row selection. Returns the selected processing ID or None."""
    config = {"Amount (₹)": st.column_config.NumberColumn(format="%.2f")}
    shown = with_decision_dot(df)
    try:
        event = show_df(shown, hide_index=True, column_config=config, on_select="rerun",
                        selection_mode="single-row", key="history_table")
        rows = event.selection.rows
        return int(df.iloc[rows[0]]["ID"]) if rows else None
    except TypeError:             # older Streamlit without row selection: use a drop-down instead
        show_df(shown, hide_index=True, column_config=config)
        ids = [None] + [int(i) for i in df["ID"]]
        return st.selectbox("Select a record to inspect", ids,
                            format_func=lambda i: "—" if i is None else f"Record #{i}")


def page_history():
    st.title("Invoice History")
    records = db.get_all_invoices()
    if not records:
        st.info("No invoices have been processed yet.")
        st.button("Go to Process Invoice", on_click=go_to, args=("Process Invoice",))
        return
    df = records_to_df(records)
    dates = pd.to_datetime(df["Processed"], errors="coerce").dt.date

    f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
    search = f1.text_input("Search", placeholder="invoice no., vendor, PO, file name or ID")
    decisions = f2.multiselect("Decision", [db.DECISION_APPROVED, db.DECISION_FLAGGED, db.DECISION_REJECTED])
    vendor = f3.selectbox("Vendor", ["All vendors"] + db.get_vendor_names())
    date_range = f4.date_input("Processed between", value=(dates.min(), dates.max()),
                               min_value=dates.min(), max_value=dates.max())

    mask = pd.Series(True, index=df.index)
    if search.strip():
        haystack = (df["ID"].astype(str) + " " + df["File"].fillna("") + " " + df["Invoice No."].fillna("") + " "
                    + df["Vendor"].fillna("") + " " + df["PO Number"].fillna("")).str.lower()
        mask &= haystack.str.contains(search.strip().lower(), regex=False)
    if decisions:
        mask &= df["Decision"].isin(decisions)
    if vendor != "All vendors":
        mask &= df["Vendor"] == vendor
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
        mask &= (dates >= date_range[0]) & (dates <= date_range[1])
    filtered = df[mask].reset_index(drop=True)

    st.caption(f"Showing **{len(filtered)}** of {len(df)} records. Tick the box at the left of a row to inspect it.")
    selected = history_table(filtered) if len(filtered) else None
    if not len(filtered):
        st.warning("No records match the current filters.")
    st.download_button("Download filtered table (CSV)", filtered.to_csv(index=False).encode("utf-8-sig"),
                       file_name="invoice_history.csv", mime="text/csv")

    if selected is not None:
        rec = db.get_invoice(selected)
        st.divider()
        st.subheader(f"Record #{selected}")
        if rec:
            st.button("Open in Processing Details", key=f"hist_open_{selected}", on_click=go_to,
                      args=("Processing Details", selected))
            render_record_details(rec, key_prefix="hist")


# ==========================================================================
# PAGE 5 - Test Scenarios
# ==========================================================================
SCENARIOS = [
    dict(file="normal_invoice.pdf", title="1. Normal invoice", expected=db.DECISION_APPROVED,
         scenario="Sharma Office Supplies bills ₹42,480 against PO-2026-1001 (₹85,000). Vendor approved, PO active, totals reconcile.",
         needs="Run this first."),
    dict(file="duplicate_invoice.pdf", title="2. Duplicate invoice", expected=db.DECISION_REJECTED,
         scenario="The same vendor submits invoice INV-SS-2026-001 a second time. It must be rejected and must not use PO balance twice.",
         needs="Needs scenario 1 processed first."),
    dict(file="po_exceeded_invoice.pdf", title="3. PO exceeded", expected=db.DECISION_REJECTED,
         scenario="Deccan Facility Services bills ₹94,400 against PO-2026-1004 (₹75,000, 2% tolerance, limit ₹76,500).",
         needs="Independent."),
    dict(file="missing_po_invoice.pdf", title="4. Missing PO", expected=db.DECISION_FLAGGED,
         scenario="Orchid Catering sends ₹23,600 with no PO number. A human has to look at it.",
         needs="Independent."),
    dict(file="split_invoice_1.pdf", title="5. Split invoice 1 of 3", expected=db.DECISION_APPROVED,
         scenario="Kaveri Logistics bills ₹60,000 against PO-2026-1003 (₹100,000, 5% tolerance). First part of a split delivery.",
         needs="Run before splits 2 and 3."),
    dict(file="split_invoice_2.pdf", title="6. Split invoice 2 of 3", expected=db.DECISION_APPROVED,
         scenario="A further ₹40,000 on the same PO. Cumulative approved becomes ₹100,000 - exactly the PO amount.",
         needs="Needs scenario 5 processed first."),
    dict(file="split_invoice_3.pdf", title="7. Split invoice 3 of 3", expected=db.DECISION_REJECTED,
         scenario="Another ₹50,000 would make the cumulative total ₹150,000, above the ₹105,000 tolerance limit.",
         needs="Needs scenarios 5 and 6 processed first."),
]


def run_scenario(filename):
    """Button callback: process one sample PDF through the real backend."""
    try:
        process_pdf_bytes(filename, (SAMPLES / filename).read_bytes())
        st.session_state["scenario_error"] = None
    except Exception as error:
        st.session_state["scenario_error"] = f"{filename}: {error}"


def run_all_scenarios():
    for sc in SCENARIOS:
        run_scenario(sc["file"])
        if st.session_state.get("scenario_error"):
            break
    st.session_state["confirm_run_all"] = False


def latest_by_filename():
    latest = {}
    for rec in db.get_all_invoices():          # newest first
        latest.setdefault(rec["filename"], rec)
    return latest


def page_scenarios():
    st.title("Test Scenarios")
    st.caption("Run the prepared sample invoices one at a time and compare the real result with the expected business outcome.")
    st.warning("These samples run through the **real** processing pipeline and are saved to your **real** invoice "
               "history. Nothing runs automatically. Some samples depend on earlier ones (a duplicate needs its "
               "original, split 2 needs split 1), and processing the same sample twice is rejected as a duplicate.")
    if st.session_state.get("scenario_error"):
        st.error(st.session_state["scenario_error"])

    latest = latest_by_filename()
    missing = [sc["file"] for sc in SCENARIOS if not (SAMPLES / sc["file"]).exists()]
    if missing:
        st.error("Sample PDFs not found in the sample_invoices folder: " + ", ".join(missing))

    # --- overview table --------------------------------------------------
    rows = []
    for sc in SCENARIOS:
        rec = latest.get(sc["file"])
        rows.append({
            "Scenario": sc["title"], "Sample file": sc["file"], "Expected": sc["expected"],
            "Actual": rec["decision"] if rec else "Not run yet",
            "Match": ("✔ Yes" if rec["decision"] == sc["expected"] else "✖ No") if rec else "—",
        })
    show_df(pd.DataFrame(rows), hide_index=True)

    # --- run everything in the recommended order --------------------------
    with st.expander("Run all seven in the recommended order"):
        st.write("Adds seven records to your invoice history, in the order shown above.")
        st.checkbox("I understand this writes to my real invoice history", key="confirm_run_all")
        st.button("Run all 7 samples", disabled=not st.session_state.get("confirm_run_all", False),
                  on_click=run_all_scenarios)

    # --- one card per scenario ----------------------------------------------
    for sc in SCENARIOS:
        rec = latest.get(sc["file"])
        with st.container(border=True):
            st.markdown(f"#### {sc['title']}")
            st.write(sc["scenario"])
            st.markdown(f"**Expected:** {status_chip_for_decision(sc['expected'])} &nbsp; "
                        f"<span class='iq-note'>{esc(sc['needs'])}</span>", unsafe_allow_html=True)
            st.button("Process this sample", key=f"run_{sc['file']}", on_click=run_scenario, args=(sc["file"],),
                      disabled=not (SAMPLES / sc["file"]).exists())
            if rec:
                matches = rec["decision"] == sc["expected"]
                st.markdown(f"**Actual:** {status_chip_for_decision(rec['decision'])} &nbsp; "
                            f"{'✔ matches the expected outcome' if matches else '✖ differs from the expected outcome'}",
                            unsafe_allow_html=True)
                if not matches:
                    st.caption("Samples depend on each other and on what is already in your history - e.g. a sample "
                               "processed twice becomes a duplicate. Open the details to see the exact reason.")
                st.write(rec["decision_explanation"])
                with st.expander(f"Validation checks (record #{rec['processing_id']})"):
                    render_checks(rec["validation_results"])
                    st.button("Open in Processing Details", key=f"sc_open_{sc['file']}", on_click=go_to,
                              args=("Processing Details", rec["processing_id"]))


def status_chip_for_decision(decision):
    colour = DECISION_COLOR.get(decision, "#334155")
    return (f'<span class="iq-chip" style="color:#fff; background:{colour};">{esc(decision)}</span>')


# ==========================================================================
# Sidebar + routing
# ==========================================================================
st.session_state.setdefault("page", "Dashboard")
with st.sidebar:
    st.markdown("## 🧾 InvoiceIQ")
    st.caption("Invoice processing & validation for Accounts Payable")
    st.radio("Navigation", PAGES, key="page", label_visibility="collapsed")
    st.divider()
    _stats = db.get_dashboard_stats()
    st.caption(f"Database: `{db.DB_PATH.name}`  \n{_stats['total_invoices']} processing records")
    st.caption("Decisions come from fixed Python rules. No AI model approves or rejects an invoice.")

{
    "Dashboard": page_dashboard,
    "Process Invoice": page_process,
    "Processing Details": page_details,
    "Invoice History": page_history,
    "Test Scenarios": page_scenarios,
}[st.session_state["page"]]()
