"""
ui_helpers.py  -  InvoiceIQ, Stage 5

Display helpers and the one piece of "glue" between the Streamlit pages and the
Stage 4 backend.  This file contains NO business rules: every decision comes from
invoice_processor.py and every number comes from database.py.
"""
import html
import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import streamlit as st

import database as db
import invoice_processor as ip

# --------------------------------------------------------------------------
# Colours / labels
# --------------------------------------------------------------------------
DECISION_COLOR = {db.DECISION_APPROVED: "#15803d", db.DECISION_FLAGGED: "#b45309", db.DECISION_REJECTED: "#b91c1c"}
DECISION_SOFT = {db.DECISION_APPROVED: "#dcfce7", db.DECISION_FLAGGED: "#fef3c7", db.DECISION_REJECTED: "#fee2e2"}
DECISION_ICON = {db.DECISION_APPROVED: "✔", db.DECISION_FLAGGED: "⚠", db.DECISION_REJECTED: "✖"}
DECISION_DOT = {db.DECISION_APPROVED: "🟢", db.DECISION_FLAGGED: "🟠", db.DECISION_REJECTED: "🔴"}

STATUS_STYLE = {            # status -> (text colour, background)
    "PASS": ("#166534", "#dcfce7"),
    "FAIL": ("#991b1b", "#fee2e2"),
    "REVIEW": ("#92400e", "#fef3c7"),
    "SKIPPED": ("#475569", "#e2e8f0"),
}
STATUS_ICON = {"PASS": "✔", "FAIL": "✖", "REVIEW": "⚠", "SKIPPED": "–"}
STATUS_WORD = {"PASS": "Passed", "FAIL": "Failed", "REVIEW": "Needs review", "SKIPPED": "Skipped"}

FIELD_LABELS = [            # (key in the extracted-fields dict, label shown to the user, is money?)
    ("vendor_name", "Vendor name", False),
    ("invoice_number", "Invoice number", False),
    ("invoice_date", "Invoice date", False),
    ("due_date", "Due date", False),
    ("po_number", "PO number", False),
    ("payment_terms", "Payment terms", False),
    ("currency", "Currency", False),
    ("subtotal", "Subtotal", True),
    ("tax_amount", "GST / tax", True),
    ("total_amount", "Invoice total", True),
]
KEY_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "po_number",
              "subtotal", "tax_amount", "total_amount"]

# The six processing stages shown on the Process Invoice page.
STAGES = [
    ("extract", "PDF text extraction"),
    ("vendor", "Vendor identification"),
    ("po", "Purchase order validation"),
    ("duplicate", "Duplicate detection"),
    ("financial", "Financial validation"),
    ("decision", "Final decision"),
]
# Which stage each check produced by invoice_processor.evaluate_invoice() belongs to.
# (Only a display grouping; a check name not listed here is shown under "financial".)
STAGE_OF_CHECK = {
    "PDF text extraction": "extract", "Invoice number extraction": "extract",
    "Invoice date extraction": "extract", "Invoice total extraction": "extract",
    "Field extraction": "extract",
    "Vendor validation": "vendor", "Vendor name consistency": "vendor",
    "Purchase order validation": "po", "Currency validation": "po",
    "Duplicate detection": "duplicate",
    "Arithmetic validation": "financial", "PO amount validation": "financial",
}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def esc(value):
    """HTML-escape anything that came from a PDF or the database before showing it."""
    return html.escape("" if value is None else str(value))


def money(value):
    if value is None or value == "":
        return "—"
    try:
        return f"₹{float(Decimal(str(value))):,.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def worst_status(statuses):
    for s in ("FAIL", "REVIEW", "PASS", "SKIPPED"):
        if s in statuses:
            return s
    return "PASS"


# --------------------------------------------------------------------------
# Page style
# --------------------------------------------------------------------------
CSS = """
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1250px;}
[data-testid="stMetric"] {background:#ffffff; border:1px solid #e2e8f0; border-radius:12px;
    padding:14px 16px; box-shadow:0 1px 2px rgba(15,23,42,.04);}
[data-testid="stMetricLabel"] {color:#64748b;}
.iq-banner {border-radius:14px; padding:18px 22px; margin:6px 0 14px 0; border:1px solid;}
.iq-banner .iq-title {font-size:1.7rem; font-weight:800; letter-spacing:.3px; margin:0;}
.iq-banner .iq-sub {font-size:1rem; margin-top:6px; color:#1e293b;}
.iq-chip {display:inline-block; padding:2px 10px; border-radius:999px; font-size:.78rem;
    font-weight:700; letter-spacing:.3px; white-space:nowrap;}
.iq-check {display:flex; gap:12px; align-items:flex-start; padding:10px 14px; margin:6px 0;
    border:1px solid #e2e8f0; border-radius:10px; background:#fff;}
.iq-check .iq-name {font-weight:700; color:#0f172a;}
.iq-check .iq-text {color:#334155; font-size:.93rem; margin-top:2px;}
.iq-stage {display:flex; gap:12px; align-items:flex-start; padding:10px 14px; margin:6px 0;
    border:1px solid #e2e8f0; border-radius:10px; background:#fff;}
.iq-stage .iq-dot {width:26px; height:26px; border-radius:50%; display:flex; align-items:center;
    justify-content:center; font-weight:800; font-size:.85rem; flex:0 0 26px;}
.iq-stage .iq-name {font-weight:700; color:#0f172a;}
.iq-stage .iq-text {color:#475569; font-size:.88rem; margin-top:2px;}
.iq-field {border:1px solid #e2e8f0; border-radius:10px; padding:10px 14px; background:#fff; margin-bottom:10px;}
.iq-field .iq-label {font-size:.75rem; color:#64748b; text-transform:uppercase; letter-spacing:.5px;}
.iq-field .iq-value {font-size:1.02rem; font-weight:600; color:#0f172a; word-break:break-word;}
.iq-field .iq-missing {color:#b91c1c; font-weight:600;}
.iq-note {color:#64748b; font-size:.85rem;}
section[data-testid="stSidebar"] {background:#f8fafc; border-right:1px solid #e2e8f0;}
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Streamlit version-safe wrappers (newer versions use width="stretch")
# --------------------------------------------------------------------------
def show_df(data, **kwargs):
    try:
        return st.dataframe(data, width="stretch", **kwargs)
    except TypeError:
        return st.dataframe(data, use_container_width=True, **kwargs)


def show_plot(figure, **kwargs):
    try:
        return st.plotly_chart(figure, width="stretch", **kwargs)
    except TypeError:
        return st.plotly_chart(figure, use_container_width=True, **kwargs)


# --------------------------------------------------------------------------
# HTML building blocks (everything user-derived goes through esc())
# --------------------------------------------------------------------------
def decision_banner(decision, explanation, heading=None):
    colour = DECISION_COLOR.get(decision, "#334155")
    soft = DECISION_SOFT.get(decision, "#f1f5f9")
    icon = DECISION_ICON.get(decision, "")
    st.markdown(
        f'<div class="iq-banner" style="background:{soft}; border-color:{colour};">'
        f'<div class="iq-title" style="color:{colour};">{icon} {esc(heading or decision)}</div>'
        f'<div class="iq-sub">{esc(explanation)}</div></div>',
        unsafe_allow_html=True)


def status_chip(status):
    colour, bg = STATUS_STYLE.get(status, ("#334155", "#e2e8f0"))
    return f'<span class="iq-chip" style="color:{colour}; background:{bg};">{esc(status)}</span>'


def render_checks(results):
    """One row per validation check with a coloured PASS / FAIL / REVIEW / SKIPPED chip."""
    if not results:
        st.info("No validation results were stored for this invoice.")
        return
    counts = {s: sum(1 for r in results if r.get("result") == s) for s in STATUS_STYLE}
    st.markdown(" &nbsp; ".join(f'{status_chip(s)} <b>{n}</b>' for s, n in counts.items() if n),
                unsafe_allow_html=True)
    rows = "".join(
        f'<div class="iq-check"><div>{status_chip(r.get("result"))}</div>'
        f'<div><div class="iq-name">{esc(r.get("check"))}</div>'
        f'<div class="iq-text">{esc(r.get("explanation"))}</div></div></div>'
        for r in results)
    st.markdown(rows, unsafe_allow_html=True)


def _display_value(key, value, is_money):
    if value in (None, ""):
        return None
    return money(value) if is_money else str(value)


def render_fields(fields):
    """Extracted fields as small cards (two per row). Missing values are marked clearly."""
    fields = fields or {}
    left, right = st.columns(2)
    for i, (key, label, is_money) in enumerate(FIELD_LABELS):
        shown = _display_value(key, fields.get(key), is_money)
        body = (f'<div class="iq-value">{esc(shown)}</div>' if shown is not None
                else '<div class="iq-value iq-missing">not found</div>')
        (left if i % 2 == 0 else right).markdown(
            f'<div class="iq-field"><div class="iq-label">{esc(label)}</div>{body}</div>',
            unsafe_allow_html=True)
    notes = fields.get("extraction_notes") or []
    if notes:
        st.warning("Extraction notes:\n\n" + "\n".join(f"- {n}" for n in notes))
    if fields.get("extraction_error"):
        st.error(fields["extraction_error"])


def render_reasoning(results):
    """Plain-language list of the checks that drove a non-approved decision."""
    failed = [r for r in results or [] if r.get("result") == "FAIL"]
    review = [r for r in results or [] if r.get("result") == "REVIEW"]
    if not failed and not review:
        st.success("Every applicable check passed, so nothing needed rejecting or reviewing.")
        return
    if failed:
        st.markdown("**Checks that caused a rejection**")
        for r in failed:
            st.markdown(f"- ✖ **{r['check']}** — {r['explanation']}")
    if review:
        st.markdown("**Checks that need a human to look**")
        for r in review:
            st.markdown(f"- ⚠ **{r['check']}** — {r['explanation']}")


# --------------------------------------------------------------------------
# Stage view (Process Invoice page)
# --------------------------------------------------------------------------
def group_checks_by_stage(results):
    grouped = {key: [] for key, _ in STAGES}
    for r in results or []:
        grouped[STAGE_OF_CHECK.get(r.get("check"), "financial")].append(r)
    return grouped


def stage_html(name, state, status=None, detail="", word=None):
    """state: pending | running | done.  status (when done): PASS/FAIL/REVIEW/SKIPPED.
    word overrides the badge text (used for the final decision: Approved / Flagged / Rejected)."""
    if state == "pending":
        dot, colour, bg, word = "·", "#94a3b8", "#f1f5f9", "Waiting"
    elif state == "running":
        dot, colour, bg, word = "…", "#1d4ed8", "#dbeafe", "Running"
    else:
        colour, bg = STATUS_STYLE.get(status, ("#334155", "#e2e8f0"))
        dot, word = STATUS_ICON.get(status, "✔"), word or STATUS_WORD.get(status, "Done")
    return (f'<div class="iq-stage"><div class="iq-dot" style="color:{colour}; background:{bg};">{dot}</div>'
            f'<div><div class="iq-name">{esc(name)} '
            f'<span class="iq-chip" style="color:{colour}; background:{bg};">{esc(word)}</span></div>'
            f'<div class="iq-text">{esc(detail)}</div></div></div>')


def extraction_summary(fields):
    found = sum(1 for k in KEY_FIELDS if (fields or {}).get(k) not in (None, ""))
    return f"Read the PDF text and found {found} of {len(KEY_FIELDS)} key fields."


def stage_detail(results):
    return " ".join(r["explanation"] for r in results) if results else ""


# --------------------------------------------------------------------------
# Backend glue
# --------------------------------------------------------------------------
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def process_pdf_bytes(filename, data, on_event=None):
    """Run the real Stage 4 backend on PDF bytes and save the result.

    on_event(name, payload) is optional and lets the page repaint the stages:
        extract_start, extract_done(fields), evaluated(evaluation),
        saving, saved(record)
    The PDF is written to a temporary folder that is always deleted afterwards
    (safe on Windows because the backend closes the file before we leave).
    """
    def emit(name, payload=None):
        if on_event:
            on_event(name, payload)

    display_name = re.split(r"[\\/]", str(filename))[-1].strip() or "invoice.pdf"   # drop any folder part (either slash)
    if not data or not data.lstrip().startswith(b"%PDF"):
        raise ValueError("This file does not look like a PDF (it has no %PDF header).")

    emit("extract_start")
    with tempfile.TemporaryDirectory(prefix="invoiceiq_") as folder:
        safe_name = _BAD_FILENAME_CHARS.sub("_", display_name)
        pdf_path = Path(folder) / safe_name
        pdf_path.write_bytes(data)
        fields = ip.extract_invoice_fields(pdf_path)          # real backend call 1
    emit("extract_done", fields)

    evaluation = ip.evaluate_invoice(fields)                   # real backend call 2 (no DB writes)
    emit("evaluated", evaluation)

    emit("saving")
    processing_id = ip.save_evaluation(display_name, fields, evaluation)   # real backend call 3
    record = db.get_invoice(processing_id)
    emit("saved", record)

    return {
        "processing_id": processing_id,
        "filename": display_name,
        "decision": record["decision"],
        "explanation": record["decision_explanation"],
        "fields": fields,
        "validation_results": record["validation_results"],
        "po_balance": evaluation.get("po_balance"),
        "record": record,
    }


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
HISTORY_COLUMNS = ["ID", "Processed", "File", "Invoice No.", "Vendor", "PO Number",
                   "Amount (₹)", "Decision", "Duplicate of"]


def records_to_df(records):
    rows = [{
        "ID": r["processing_id"],
        "Processed": r["processed_at"],
        "File": r["filename"],
        "Invoice No.": r["invoice_number"],
        "Vendor": r["vendor_name"],
        "PO Number": r["po_number"],
        "Amount (₹)": r["total_amount"],
        "Decision": r["decision"],
        "Duplicate of": r["duplicate_of"],
    } for r in records]
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def with_decision_dot(df):
    """Copy of df whose Decision column has a coloured dot in front (for display only)."""
    shown = df.copy()
    shown["Decision"] = shown["Decision"].map(lambda d: f"{DECISION_DOT.get(d, '')} {d}")
    return shown
