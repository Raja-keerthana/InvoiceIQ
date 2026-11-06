"""
database.py  -  InvoiceIQ, Stage 3

Everything that touches SQLite lives in this file:
  * creating the tables (safe to run many times)
  * saving a finished processing record (the audit trail)
  * duplicate-invoice lookup
  * cumulative PO tracking (only APPROVED invoices use up a PO)
  * history, filters and dashboard numbers

This file knows nothing about PDFs, extraction or Streamlit.

Money rules
  * Amounts are rounded to 2 decimal places using Decimal (not float maths),
    so 33,333.33 x 3 is exactly 99,999.99.
  * Amounts are stored as plain numbers in the database and handed back as
    ordinary Python floats rounded to 2 places.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# --------------------------------------------------------------------------
# Settings shared with the rest of the app
# --------------------------------------------------------------------------
# The database file sits in the main InvoiceIQ folder, next to this file.
DB_PATH = Path(__file__).resolve().parent / "invoice_history.db"

# The three (and only three) possible decisions. invoice_processor.py will
# import these, so the spelling is defined in exactly one place.
DECISION_APPROVED = "APPROVED"
DECISION_FLAGGED = "FLAGGED FOR REVIEW"
DECISION_REJECTED = "REJECTED"
VALID_DECISIONS = (DECISION_APPROVED, DECISION_FLAGGED, DECISION_REJECTED)

TWO_PLACES = Decimal("0.01")


class DuplicateInvoiceError(Exception):
    """Raised when saving would create a second 'original' record for the
    same vendor + invoice number (for example, approving it a second time)."""


# --------------------------------------------------------------------------
# Table definitions
# --------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS invoices (
    processing_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    filename             TEXT    NOT NULL,
    vendor_id            TEXT,
    vendor_name          TEXT,
    vendor_key           TEXT    NOT NULL DEFAULT '',   -- vendor ID, or NAME:<name> if unknown
    invoice_number       TEXT,
    invoice_key          TEXT    NOT NULL DEFAULT '',   -- invoice number, upper-case, no spaces
    invoice_date         TEXT,
    due_date             TEXT,
    po_number            TEXT,
    currency             TEXT,
    subtotal             REAL,
    tax_amount           REAL,
    total_amount         REAL,
    processed_at         TEXT    NOT NULL,
    decision             TEXT    NOT NULL,
    decision_explanation TEXT    NOT NULL,
    duplicate_of         INTEGER REFERENCES invoices(processing_id),
    validation_json      TEXT    NOT NULL DEFAULT '[]',
    extracted_json       TEXT    NOT NULL DEFAULT '{}',

    -- Rules the database enforces by itself, even if the Python code has a bug:
    CHECK (decision IN ('APPROVED', 'FLAGGED FOR REVIEW', 'REJECTED')),
    CHECK (decision <> 'APPROVED' OR total_amount IS NOT NULL),
    CHECK (duplicate_of IS NULL OR decision = 'REJECTED')   -- a duplicate can never be APPROVED
);

-- Only ONE original record may exist per vendor + invoice number.
-- Duplicate submissions are stored too (for the audit trail) but they carry
-- duplicate_of, so they are exempt. This is what stops an invoice from being
-- approved - and therefore consuming PO balance - twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_original
    ON invoices (vendor_key, invoice_key)
    WHERE duplicate_of IS NULL AND vendor_key <> '' AND invoice_key <> '';

CREATE INDEX IF NOT EXISTS idx_invoices_po         ON invoices (po_number, decision);
CREATE INDEX IF NOT EXISTS idx_invoices_decision   ON invoices (decision);
CREATE INDEX IF NOT EXISTS idx_invoices_processed  ON invoices (processed_at);

CREATE TABLE IF NOT EXISTS audit_log (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    processing_id INTEGER NOT NULL REFERENCES invoices(processing_id),
    event_time    TEXT    NOT NULL,
    event_type    TEXT    NOT NULL,      -- PROCESSED, MANUAL_CORRECTION, ...
    details_json  TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_processing ON audit_log (processing_id);
"""

# Columns written when a record is saved. These names are fixed constants in
# this file (never user input), so building the SQL text from them is safe.
# The VALUES themselves always go through ? / :name parameters.
_COLUMNS = [
    "filename", "vendor_id", "vendor_name", "vendor_key", "invoice_number", "invoice_key",
    "invoice_date", "due_date", "po_number", "currency", "subtotal", "tax_amount",
    "total_amount", "processed_at", "decision", "decision_explanation", "duplicate_of",
    "validation_json", "extracted_json",
]
# When a FLAGGED invoice is reprocessed, the original timestamp and filename stay.
_UPDATE_COLUMNS = [c for c in _COLUMNS if c not in ("processed_at", "filename")]

_ready_databases = set()     # remembers which database files already have their tables


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------
@contextmanager
def get_connection():
    """Open the database, hand it over, then commit (or roll back on error) and close.

        with get_connection() as conn:
            conn.execute("SELECT ...", (value,))

    Tables are created automatically the first time a database file is used.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if str(DB_PATH) not in _ready_databases:
            conn.executescript(SCHEMA_SQL)
            _ready_databases.add(str(DB_PATH))
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the database file and tables if they do not exist yet.
    Safe to call as often as you like - existing data is never touched."""
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
    return DB_PATH


def reset_database():
    """DELETE ALL invoice records and audit events (used by the Test Scenarios
    page and by the tests). The tables themselves are kept."""
    with get_connection() as conn:
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM invoices")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices', 'audit_log')")


# --------------------------------------------------------------------------
# Small cleaning helpers
# --------------------------------------------------------------------------
def _clean(value):
    """Strip spaces; turn None and empty text into None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_money(value):
    """Convert a number or text like '1,18,000.50' to a Decimal with 2 places.
    Returns None for missing values and raises ValueError for rubbish."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value == "":
            return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Not a valid amount: {value!r}")
    if not amount.is_finite():
        raise ValueError(f"Not a valid amount: {value!r}")
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _money_float(value):
    amount = to_money(value)
    return None if amount is None else float(amount)


def _stored_money(value):
    """Turn a number read from the database back into an exact 2-place Decimal."""
    return Decimal(str(value or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _iso_date(value):
    """Accept a date, a datetime or text; return 'YYYY-MM-DD' text (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _clean(value)


def _now():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def make_invoice_key(invoice_number):
    """'inv-ss- 2026-001 ' -> 'INV-SS-2026-001'. Returns '' if there is no number."""
    cleaned = _clean(invoice_number)
    return "" if cleaned is None else re.sub(r"\s+", "", cleaned).upper()


def make_vendor_key(vendor_id, vendor_name):
    """Use the vendor ID when we have one; otherwise fall back to the vendor name."""
    vid = _clean(vendor_id)
    if vid:
        return vid.upper()
    name = _clean(vendor_name)
    if name:
        return "NAME:" + " ".join(name.upper().split())
    return ""


def _row_to_dict(row):
    """Turn a database row into a normal dictionary, unpacking the JSON columns."""
    record = dict(row)
    record["validation_results"] = json.loads(record.pop("validation_json", None) or "[]")
    record["extracted_fields"] = json.loads(record.pop("extracted_json", None) or "{}")
    return record


# --------------------------------------------------------------------------
# Building a record that is safe to store
# --------------------------------------------------------------------------
def _prepare_record(record):
    """Check and clean a record dictionary. Returns a dict with one entry per column.

    Expected keys (all optional except filename, decision, decision_explanation):
      filename, vendor_id, vendor_name, invoice_number, invoice_date, due_date,
      po_number, currency, subtotal, tax_amount, total_amount, decision,
      decision_explanation, validation_results (list), extracted_fields (dict),
      duplicate_of (processing_id of the earlier invoice), processed_at
    """
    filename = _clean(record.get("filename"))
    if filename is None:
        raise ValueError("A processing record needs a filename.")

    decision = record.get("decision")
    if decision not in VALID_DECISIONS:
        raise ValueError(f"Decision must be one of {VALID_DECISIONS}, not {decision!r}.")

    explanation = _clean(record.get("decision_explanation"))
    if explanation is None:
        raise ValueError("A processing record needs a decision explanation.")

    total = _money_float(record.get("total_amount"))
    if decision == DECISION_APPROVED and total is None:
        raise ValueError("An APPROVED invoice must have a total amount.")

    duplicate_of = record.get("duplicate_of")
    if duplicate_of is not None and decision != DECISION_REJECTED:
        raise ValueError("A duplicate invoice can only be saved as REJECTED.")

    vendor_id = _clean(record.get("vendor_id"))
    vendor_name = _clean(record.get("vendor_name"))
    invoice_number = _clean(record.get("invoice_number"))
    po_number = _clean(record.get("po_number"))
    currency = _clean(record.get("currency"))

    return {
        "filename": filename,
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "vendor_key": make_vendor_key(vendor_id, vendor_name),
        "invoice_number": invoice_number,
        "invoice_key": make_invoice_key(invoice_number),
        "invoice_date": _iso_date(record.get("invoice_date")),
        "due_date": _iso_date(record.get("due_date")),
        "po_number": po_number.upper() if po_number else None,
        "currency": currency.upper() if currency else None,
        "subtotal": _money_float(record.get("subtotal")),
        "tax_amount": _money_float(record.get("tax_amount")),
        "total_amount": total,
        "processed_at": _clean(record.get("processed_at")) or _now(),
        "decision": decision,
        "decision_explanation": explanation,
        "duplicate_of": duplicate_of,
        "validation_json": json.dumps(record.get("validation_results") or [],
                                      ensure_ascii=False, default=str),
        "extracted_json": json.dumps(record.get("extracted_fields") or {},
                                     ensure_ascii=False, default=str),
    }


def _write_audit_event(conn, processing_id, event_type, details):
    conn.execute(
        "INSERT INTO audit_log (processing_id, event_time, event_type, details_json) "
        "VALUES (?, ?, ?, ?)",
        (processing_id, _now(), event_type, json.dumps(details or {}, ensure_ascii=False, default=str)),
    )


# --------------------------------------------------------------------------
# Saving and updating records
# --------------------------------------------------------------------------
def save_processing_record(record):
    """Save a finished processing record and return its new processing_id.

    Call this ONLY after every check (including the duplicate check) is done.
    Everything happens in one transaction: either the record AND its audit
    event are saved, or nothing is.
    """
    values = _prepare_record(record)
    columns = ", ".join(_COLUMNS)
    placeholders = ", ".join(":" + c for c in _COLUMNS)
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")          # lock the database while we write
            cursor = conn.execute(f"INSERT INTO invoices ({columns}) VALUES ({placeholders})", values)
            processing_id = cursor.lastrowid
            _write_audit_event(conn, processing_id, "PROCESSED", {
                "filename": values["filename"],
                "decision": values["decision"],
                "duplicate_of": values["duplicate_of"],
            })
    except sqlite3.IntegrityError as error:
        if "UNIQUE" in str(error).upper():
            raise DuplicateInvoiceError(
                f"An original record already exists for vendor '{values['vendor_key']}' "
                f"and invoice number '{values['invoice_number']}'. Nothing was saved.") from error
        raise
    return processing_id


def update_processing_record(processing_id, record, corrections=None):
    """Replace the result of an invoice that is FLAGGED FOR REVIEW after a human
    corrected it and it was run through the checks again.

    corrections is an optional dictionary describing what the reviewer changed,
    e.g. {"po_number": {"old": None, "new": "PO-2026-1011"}}. It is written to
    the audit trail. Records that are already APPROVED or REJECTED cannot be edited.
    """
    values = _prepare_record(record)
    assignments = ", ".join(f"{c} = :{c}" for c in _UPDATE_COLUMNS)
    values["processing_id"] = processing_id
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT decision FROM invoices WHERE processing_id = ?",
                                    (processing_id,)).fetchone()
            if existing is None:
                raise ValueError(f"No processing record with ID {processing_id}.")
            if existing["decision"] != DECISION_FLAGGED:
                raise ValueError("Only invoices FLAGGED FOR REVIEW can be reprocessed "
                                 f"(this one is {existing['decision']}).")
            conn.execute(f"UPDATE invoices SET {assignments} WHERE processing_id = :processing_id", values)
            _write_audit_event(conn, processing_id,
                               "MANUAL_CORRECTION" if corrections else "REPROCESSED", {
                                   "old_decision": existing["decision"],
                                   "new_decision": values["decision"],
                                   "corrections": corrections or {},
                               })
    except sqlite3.IntegrityError as error:
        if "UNIQUE" in str(error).upper():
            raise DuplicateInvoiceError(
                "Another original record already exists for this vendor and invoice number. "
                "Nothing was changed.") from error
        raise


def add_audit_event(processing_id, event_type, details=None):
    """Add a free-form event (for example a reviewer's note) to the audit trail."""
    with get_connection() as conn:
        _write_audit_event(conn, processing_id, event_type, details)


def get_audit_events(processing_id):
    """All audit events for one record, oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE processing_id = ? ORDER BY event_id", (processing_id,)
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["details"] = json.loads(event.pop("details_json") or "{}")
        events.append(event)
    return events


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------
def find_duplicate(vendor_id, vendor_name, invoice_number, exclude_processing_id=None):
    """Has this vendor already submitted this invoice number?

    Returns the earlier ORIGINAL record (as a dictionary) or None.
      * Invoice numbers are compared ignoring capital letters and spaces.
      * If the invoice number (or the vendor) is missing we cannot tell, so
        None is returned - the processor must flag that invoice for review
        instead of assuming it is unique.
      * Records saved as duplicates are never "the original", so a third
        submission points back at the first one.
      * exclude_processing_id lets a flagged invoice be re-checked without
        matching itself.
    """
    vendor_key = make_vendor_key(vendor_id, vendor_name)
    invoice_key = make_invoice_key(invoice_number)
    if not vendor_key or not invoice_key:
        return None

    sql = ("SELECT * FROM invoices "
           "WHERE vendor_key = ? AND invoice_key = ? AND duplicate_of IS NULL")
    params = [vendor_key, invoice_key]
    if exclude_processing_id is not None:
        sql += " AND processing_id <> ?"
        params.append(exclude_processing_id)
    sql += " ORDER BY processing_id LIMIT 1"

    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return _row_to_dict(row) if row else None


# --------------------------------------------------------------------------
# Cumulative PO tracking
# --------------------------------------------------------------------------
def calculate_max_allowed(po_amount, tolerance_percentage):
    """Highest cumulative amount allowed on a PO:
           PO amount x (1 + tolerance% / 100)      e.g. 1,00,000 at 5% -> 1,05,000.00"""
    amount = to_money(po_amount)
    tolerance = to_money(tolerance_percentage if tolerance_percentage is not None else 0)
    if amount is None:
        raise ValueError("PO amount is required.")
    if tolerance < 0:
        raise ValueError("Tolerance percentage cannot be negative.")
    limit = (amount * (Decimal("1") + tolerance / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return float(limit)


def get_po_consumed(po_number):
    """Sum of the totals of APPROVED invoices for this PO (duplicates and
    anything FLAGGED or REJECTED are never counted)."""
    po = _clean(po_number)
    if po is None:
        return 0.0
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT total_amount FROM invoices "
            "WHERE po_number = ? AND decision = ? AND duplicate_of IS NULL",
            (po.upper(), DECISION_APPROVED),
        ).fetchall()
    return float(sum((_stored_money(r["total_amount"]) for r in rows), Decimal("0")))


def get_po_invoices(po_number):
    """The APPROVED invoices that have used up part of this PO, oldest first."""
    po = _clean(po_number)
    if po is None:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT processing_id, invoice_number, invoice_date, total_amount, processed_at "
            "FROM invoices WHERE po_number = ? AND decision = ? AND duplicate_of IS NULL "
            "ORDER BY processing_id",
            (po.upper(), DECISION_APPROVED),
        ).fetchall()
    return [dict(r) for r in rows]


def get_po_balance(po_number, po_amount, tolerance_percentage=0):
    """Everything the validator needs to know about one PO's balance.

      consumed                 approved so far
      remaining_balance        PO amount - consumed
      max_allowed_cumulative   PO amount plus its tolerance
      remaining_with_tolerance max_allowed_cumulative - consumed
                               (the most a NEW invoice may add up to)
    All values are rounded to 2 decimal places.
    """
    amount = to_money(po_amount)
    if amount is None:
        raise ValueError("PO amount is required.")
    consumed = _stored_money(get_po_consumed(po_number))
    max_allowed = to_money(calculate_max_allowed(amount, tolerance_percentage))
    return {
        "po_number": (_clean(po_number) or "").upper(),
        "po_amount": float(amount),
        "tolerance_percentage": float(to_money(tolerance_percentage or 0)),
        "consumed": float(consumed),
        "remaining_balance": float(amount - consumed),
        "max_allowed_cumulative": float(max_allowed),
        "remaining_with_tolerance": float(max_allowed - consumed),
        "approved_invoice_count": len(get_po_invoices(po_number)),
    }


# --------------------------------------------------------------------------
# Reading history
# --------------------------------------------------------------------------
def get_invoice(processing_id):
    """One processing record by its ID, or None."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE processing_id = ?", (processing_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_invoices(decision=None, vendor_name=None, date_from=None, date_to=None, limit=None):
    """Records (newest first), optionally filtered by decision, exact vendor
    name and processing date range (dates are inclusive)."""
    sql = "SELECT * FROM invoices WHERE 1 = 1"
    params = []
    if decision:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"Decision must be one of {VALID_DECISIONS}, not {decision!r}.")
        sql += " AND decision = ?"
        params.append(decision)
    if vendor_name:
        sql += " AND vendor_name = ?"
        params.append(vendor_name)
    if date_from:
        sql += " AND substr(processed_at, 1, 10) >= ?"
        params.append(_iso_date(date_from))
    if date_to:
        sql += " AND substr(processed_at, 1, 10) <= ?"
        params.append(_iso_date(date_to))
    sql += " ORDER BY processed_at DESC, processing_id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_invoices():
    """Every processing record, newest first."""
    return get_invoices()


def get_vendor_names():
    """Distinct vendor names in the history (for a filter drop-down)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT vendor_name FROM invoices "
                            "WHERE vendor_name IS NOT NULL ORDER BY vendor_name").fetchall()
    return [r["vendor_name"] for r in rows]


# --------------------------------------------------------------------------
# Dashboard numbers
# --------------------------------------------------------------------------
def get_dashboard_stats():
    """Headline numbers, always calculated from the stored records.

      total_invoices        every processing record (duplicates included)
      approval_rate         approved / total_invoices x 100, 1 decimal place
      total_value_processed total of all records EXCEPT duplicate submissions,
                            so the same invoice is never counted twice
      approved_value        total of APPROVED invoices
    """
    with get_connection() as conn:
        row = conn.execute("""
            SELECT COUNT(*)                                                    AS total,
                   COALESCE(SUM(decision = 'APPROVED'), 0)                     AS approved,
                   COALESCE(SUM(decision = 'FLAGGED FOR REVIEW'), 0)           AS flagged,
                   COALESCE(SUM(decision = 'REJECTED'), 0)                     AS rejected,
                   COUNT(duplicate_of)                                         AS duplicates,
                   COALESCE(SUM(CASE WHEN duplicate_of IS NULL THEN total_amount END), 0) AS value_all,
                   COALESCE(SUM(CASE WHEN decision = 'APPROVED' THEN total_amount END), 0) AS value_approved
            FROM invoices
        """).fetchone()
    total = row["total"]
    return {
        "total_invoices": total,
        "approved": row["approved"],
        "flagged": row["flagged"],
        "rejected": row["rejected"],
        "duplicates_detected": row["duplicates"],
        "approval_rate": round(row["approved"] / total * 100, 1) if total else 0.0,
        "total_value_processed": round(float(row["value_all"]), 2),
        "approved_value": round(float(row["value_approved"]), 2),
    }


def get_processing_trend():
    """Rows of {day, decision, count} for a processing-trend chart."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT substr(processed_at, 1, 10) AS day, decision, COUNT(*) AS count "
            "FROM invoices GROUP BY day, decision ORDER BY day, decision"
        ).fetchall()
    return [dict(r) for r in rows]


def get_amounts_by_vendor():
    """Rows of {vendor, invoices, total_value, approved_value} (duplicates excluded)."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT COALESCE(vendor_name, 'Unknown vendor') AS vendor,
                   COUNT(*)                                 AS invoices,
                   COALESCE(SUM(total_amount), 0)           AS total_value,
                   COALESCE(SUM(CASE WHEN decision = 'APPROVED' THEN total_amount END), 0) AS approved_value
            FROM invoices
            WHERE duplicate_of IS NULL
            GROUP BY vendor
            ORDER BY total_value DESC
        """).fetchall()
    return [{"vendor": r["vendor"], "invoices": r["invoices"],
             "total_value": round(float(r["total_value"]), 2),
             "approved_value": round(float(r["approved_value"]), 2)} for r in rows]