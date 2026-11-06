"""
invoice_processor.py  -  InvoiceIQ, Stage 4

Reads one invoice PDF, validates it with plain Python rules, decides, and saves
the result through database.py.  NO LLM / API is used for any decision.

Three steps (the Streamlit UI in Stage 5 calls them one by one to show live stages):

    fields     = extract_invoice_fields(pdf_path)        # PDF text  -> fields
    evaluation = evaluate_invoice(fields)                # fields    -> checks + decision (no DB writes)
    pid        = save_evaluation(filename, fields, evaluation)   # -> invoice_history.db

or all at once:   process_invoice(pdf_path)

Decision rules (first match wins)
    REJECTED            duplicate invoice, vendor not approved / not in vendor master,
                        inactive PO, vendor-PO mismatch, cumulative amount > PO + tolerance
    FLAGGED FOR REVIEW  anything critical missing or uncertain (vendor, invoice no., date,
                        total, PO number, unknown PO, arithmetic mismatch, currency mismatch,
                        conflicting vendor names) or cumulative amount above the PO amount
                        but still inside tolerance
    APPROVED            everything passes and cumulative amount <= original PO amount

Only APPROVED invoices consume PO balance (that is enforced in database.py).
"""
import csv
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pymupdf

import database as db

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
VENDORS_FILE = DATA / "vendors.csv"
POS_FILE = DATA / "purchase_orders.csv"

ARITHMETIC_TOLERANCE = Decimal("1.00")     # subtotal + tax may differ from total by up to Rs 1


# ==========================================================================
# 1. EXTRACTION
# ==========================================================================
# In these PDFs a label and its value sit on SEPARATE lines:
#       Subtotal
#       Rs. 36,000.00
# so extraction works line by line: find a label line, then take the value
# from the rest of that line, or else from the next non-empty line.
# Two label styles exist in the samples ("Invoice No:" / "Grand Total" and
# "Invoice #" / "Amount Due"); several other common spellings are included too.

_AMOUNT_VALUE = r"(?:INR|Rs\.?|₹)?\s*\d[\d,]*(?:\.\d{1,2})?(?:\s*/-)?"
_DATE_VALUE = (r"\d{1,2}[-/. ][A-Za-z]{3,9}[-/. ,]+\d{2,4}"
               r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
               r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
_ID_VALUE = r"[A-Za-z0-9][A-Za-z0-9\-/_.]{2,39}"
_END = r"(?![A-Za-z0-9])"          # a label must not run on into more letters/digits

_DATE_FORMATS = ["%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y", "%d/%b/%Y", "%d.%b.%Y",
                 "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
                 "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]


def _read_csv(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _labelled_value(lines, label, value_pattern, must_have_digit=False):
    """Return the value that follows the first line starting with `label`."""
    label_re = re.compile(rf"^(?:{label}){_END}\s*[:\-]?\s*(.*)$", re.IGNORECASE)
    for i, line in enumerate(lines):
        m = label_re.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest:                                   # value on the same line
            candidate = rest
        elif i + 1 < len(lines):                   # value on the next line
            candidate = lines[i + 1]
        else:
            continue
        if re.fullmatch(value_pattern, candidate, re.IGNORECASE) and \
                (not must_have_digit or re.search(r"\d", candidate)):
            return candidate
    return None


def _parse_amount(text):
    if text is None:
        return None
    cleaned = re.sub(r"(?i)INR|Rs\.?|₹|/-|\s", "", text).replace(",", "")
    try:
        return db.to_money(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_date(text):
    """'12-Oct-2026' -> '2026-10-12'.  Returns None if it cannot be read."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text.strip().replace(",", " ")).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm_name(name):
    return re.sub(r"[^a-z0-9&]+", " ", name.casefold()).strip()


def _extract_vendor(lines):
    """Return (vendor_name, signature_name).  Letterhead = first line of the invoice;
    signature = the 'for <Vendor>' line next to 'Authorised Signatory'."""
    labelled = _labelled_value(
        lines, r"vendor\s*name|supplier\s*name|supplier|vendor|sold\s*by|from",
        r"[A-Za-z][^\n]{2,100}")

    letterhead = None
    if lines:
        first = lines[0]
        generic = re.fullmatch(r"(?i)(tax\s+)?invoice|bill|receipt|page\s*\d*", first)
        if re.search(r"[A-Za-z]{3}", first) and not generic \
                and not re.fullmatch(_AMOUNT_VALUE, first, re.IGNORECASE):
            letterhead = first

    signature = None
    for i, line in enumerate(lines):
        if re.search(r"(?i)authori[sz]ed\s+signator", line):
            for j in (i - 1, i + 1, i + 2):
                if 0 <= j < len(lines):
                    m = re.fullmatch(r"(?i)for\s+(.{3,100})", lines[j])
                    if m:
                        signature = m.group(1).strip()
                        break
            if signature:
                break
    return (labelled or letterhead or signature), signature


def _extract_total(lines):
    priorities = [
        r"grand\s*total", r"invoice\s*total", r"total\s*amount",
        r"total\s*payable|net\s*payable|amount\s*payable",
        r"amount\s*due|balance\s*due", r"total",
    ]
    for label in priorities:
        value = _labelled_value(lines, label, _AMOUNT_VALUE)
        if value is not None:
            return _parse_amount(value)
    return None


def _extract_tax(lines):
    """GST / Tax line. If only CGST/SGST/IGST components exist they are added up."""
    rate = r"(?:\s*[@(]?\s*\d{1,2}(?:\.\d+)?\s*%\s*\)?)?"
    total_label = rf"(?:total\s+)?(?:gst|vat|tax(?:\s*amount)?){rate}"
    component_label = rf"(?:cgst|sgst|igst|utgst){rate}"
    whole = None
    parts = []
    for label, is_component in ((total_label, False), (component_label, True)):
        label_re = re.compile(rf"^{label}{_END}\s*[:\-]?\s*(.*)$", re.IGNORECASE)
        for i, line in enumerate(lines):
            m = label_re.match(line)
            if not m:
                continue
            rest = m.group(1).strip()
            candidate = rest if rest else (lines[i + 1] if i + 1 < len(lines) else "")
            if re.fullmatch(_AMOUNT_VALUE, candidate, re.IGNORECASE):
                amount = _parse_amount(candidate)
                if is_component:
                    parts.append(amount)
                elif whole is None:
                    whole = amount
    if whole is not None:
        return whole
    if parts:
        return sum(parts, Decimal("0"))
    return None


def extract_fields_from_text(text):
    """Turn the plain text of an invoice into a dictionary of fields."""
    lines = _lines(text)
    notes = []

    vendor_name, signature = _extract_vendor(lines)
    if vendor_name and signature and _norm_name(vendor_name) != _norm_name(signature):
        notes.append(f"Vendor name conflict: header says '{vendor_name}' but signature says '{signature}'.")

    invoice_date_raw = _labelled_value(
        lines, r"invoice\s*date|inv\.?\s*date|bill\s*date|date\s*of\s*invoice|date", _DATE_VALUE)
    due_date_raw = _labelled_value(
        lines, r"due\s*date|due\s*by|due\s*on|payment\s*due(?:\s*date)?", _DATE_VALUE)
    invoice_date, due_date = _parse_date(invoice_date_raw), _parse_date(due_date_raw)
    if invoice_date_raw and not invoice_date:
        notes.append(f"Invoice date text '{invoice_date_raw}' could not be parsed.")

    po_number = _labelled_value(
        lines,
        r"(?:customer\s*)?(?:purchase\s*order|p\.?\s?o\.?)\s*(?:number|no\.?|num\.?|#|ref(?:erence)?)"
        r"|purchase\s*order|customer\s*po",
        r"[A-Za-z0-9][A-Za-z0-9\-/_.]{3,29}", must_have_digit=True)

    currency_label = _labelled_value(lines, r"currency", r"[A-Za-z]{3}")
    if currency_label:
        currency = currency_label.upper()
    elif re.search(r"\bINR\b|\bRs\.?|₹", text, re.IGNORECASE):
        currency = "INR"
    else:
        currency = None

    invoice_number = _labelled_value(
        lines, r"(?:invoice|inv|bill)\s*(?:number|no\.?|num\.?|#)", _ID_VALUE, must_have_digit=True)

    return {
        "vendor_name": vendor_name.strip() if vendor_name else None,
        "vendor_name_signature": signature,
        "invoice_number": invoice_number.strip() if invoice_number else None,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "po_number": po_number.strip().upper() if po_number else None,
        "payment_terms": _labelled_value(lines, r"payment\s*terms|terms", r"[A-Za-z0-9 ]{2,30}"),
        "subtotal": _parse_amount(_labelled_value(
            lines, r"sub\s*-?\s*total|taxable\s*(?:value|amount)|total\s*before\s*tax", _AMOUNT_VALUE)),
        "tax_amount": _extract_tax(lines),
        "total_amount": _extract_total(lines),
        "currency": currency,
        "extraction_notes": notes,
        "raw_text": text,
        "extraction_error": None,
    }


def extract_invoice_fields(pdf_path):
    """Read a text-based PDF and extract its fields. Scanned PDFs (no text) are
    returned with an extraction_error so they get flagged for human review."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(path)
    with pymupdf.open(path) as doc:
        text = "\n".join(page.get_text("text") for page in doc).strip()

    if not text:
        return {
            "vendor_name": None, "vendor_name_signature": None, "invoice_number": None,
            "invoice_date": None, "due_date": None, "po_number": None, "payment_terms": None,
            "subtotal": None, "tax_amount": None, "total_amount": None, "currency": None,
            "extraction_notes": [], "raw_text": "",
            "extraction_error": "No selectable text found; scanned PDF requires OCR.",
        }
    return extract_fields_from_text(text)


# ==========================================================================
# 2. VALIDATION AND DECISION  (deterministic rules, no database writes)
# ==========================================================================
def _check(name, status, explanation):
    return {"check": name, "result": status, "explanation": explanation}


def _inr(amount):
    return f"₹{amount:,.2f}"


def evaluate_invoice(fields, exclude_processing_id=None):
    """Run every check on an extracted invoice and decide.

    Returns a dictionary with: decision, explanation, validation_results,
    vendor, po, duplicate, po_balance.   Nothing is written to the database.
    exclude_processing_id is for re-checking a FLAGGED invoice after a human
    correction (so it does not match itself as a duplicate).
    """
    vendors = _read_csv(VENDORS_FILE)
    purchase_orders = _read_csv(POS_FILE)
    checks, reject, flag = [], [], []

    # ---- Vendor -----------------------------------------------------------
    vendor = None
    name = fields.get("vendor_name")
    if name:
        key = _norm_name(name)
        vendor = next((v for v in vendors if _norm_name(v["vendor_name"]) == key), None)

    if not name:
        checks.append(_check("Vendor validation", "REVIEW", "Vendor name could not be extracted."))
        flag.append("Vendor name could not be extracted.")
    elif vendor is None:
        checks.append(_check("Vendor validation", "FAIL",
                             f"'{name}' is not in the approved vendor master."))
        reject.append(f"Vendor '{name}' is not in the vendor master (not approved).")
    elif vendor["approved_status"].strip().upper() != "APPROVED":
        checks.append(_check("Vendor validation", "FAIL",
                             f"Vendor {vendor['vendor_name']} is {vendor['approved_status']}."))
        reject.append(f"Vendor {vendor['vendor_name']} is not approved.")
    else:
        checks.append(_check("Vendor validation", "PASS",
                             f"Matched approved vendor {vendor['vendor_name']} ({vendor['vendor_id']})."))

    for note in fields.get("extraction_notes") or []:
        if note.startswith("Vendor name conflict"):
            checks.append(_check("Vendor name consistency", "REVIEW", note))
            flag.append("Vendor name on the invoice is inconsistent.")

    # ---- Purchase order ---------------------------------------------------
    po = None
    po_ok = False                     # PO exists, ACTIVE and belongs to this vendor
    po_number = fields.get("po_number")
    if not po_number:
        checks.append(_check("Purchase order validation", "REVIEW", "PO number is missing from the invoice."))
        flag.append("PO number is missing.")
    else:
        po = next((p for p in purchase_orders
                   if p["po_number"].strip().upper() == po_number.upper()), None)
        if po is None:
            checks.append(_check("Purchase order validation", "REVIEW",
                                 f"PO {po_number} was not found in the purchase order file."))
            flag.append(f"PO {po_number} was not found, so it cannot be validated.")
        else:
            problems = []
            if po["po_status"].strip().upper() != "ACTIVE":
                problems.append("Purchase order is inactive.")
                reject.append(f"PO {po['po_number']} is inactive.")
            if vendor is not None and po["vendor_id"] != vendor["vendor_id"]:
                problems.append(f"PO belongs to vendor {po['vendor_id']}, not {vendor['vendor_id']}.")
                reject.append(f"Vendor and PO do not match (PO {po['po_number']} belongs to {po['vendor_id']}).")
            if problems:
                checks.append(_check("Purchase order validation", "FAIL", " ".join(problems)))
            else:
                po_ok = vendor is not None
                checks.append(_check("Purchase order validation", "PASS",
                                     "PO exists, is active, and matches the vendor." if vendor
                                     else "PO exists and is active (vendor not confirmed)."))
            invoice_currency = (fields.get("currency") or "").upper()
            if po.get("currency") and invoice_currency and invoice_currency != po["currency"].upper():
                checks.append(_check("Currency validation", "REVIEW",
                                     f"Invoice currency {invoice_currency} differs from PO currency {po['currency']}."))
                flag.append("Invoice currency differs from the PO currency.")
                po_ok = False

    # ---- Critical fields ----------------------------------------------------
    for key, label in (("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"),
                       ("total_amount", "Invoice total")):
        if fields.get(key) in (None, ""):
            checks.append(_check(f"{label} extraction", "REVIEW", f"{label} could not be extracted."))
            flag.append(f"{label} is missing or unreadable.")
    for note in fields.get("extraction_notes") or []:
        if not note.startswith("Vendor name conflict"):
            checks.append(_check("Field extraction", "REVIEW", note))
            flag.append(note)
    if fields.get("extraction_error"):
        checks.append(_check("PDF text extraction", "REVIEW", fields["extraction_error"]))
        flag.append(fields["extraction_error"])

    # ---- Arithmetic ---------------------------------------------------------
    subtotal, tax, total = fields.get("subtotal"), fields.get("tax_amount"), fields.get("total_amount")
    if None not in (subtotal, tax, total):
        difference = abs((subtotal + tax) - total)
        if difference <= ARITHMETIC_TOLERANCE:
            checks.append(_check("Arithmetic validation", "PASS",
                                 f"Subtotal {_inr(subtotal)} + tax {_inr(tax)} = total {_inr(total)} (within ₹1)."))
        else:
            checks.append(_check("Arithmetic validation", "REVIEW",
                                 f"Subtotal + tax = {_inr(subtotal + tax)} but total is {_inr(total)} "
                                 f"(difference {_inr(difference)})."))
            flag.append("Subtotal, tax and total do not reconcile.")
    else:
        checks.append(_check("Arithmetic validation", "REVIEW", "Subtotal, tax or total is missing."))
        flag.append("Financial fields are incomplete.")

    # ---- Duplicate detection --------------------------------------------------
    duplicate = None
    if fields.get("invoice_number") and (vendor or name):
        duplicate = db.find_duplicate(vendor["vendor_id"] if vendor else None, name,
                                      fields["invoice_number"],
                                      exclude_processing_id=exclude_processing_id)
    if duplicate:
        checks.append(_check("Duplicate detection", "FAIL",
                             f"Invoice {fields['invoice_number']} was already submitted by this vendor "
                             f"(processing record #{duplicate['processing_id']}, "
                             f"{duplicate['decision']})."))
        reject.append(f"Duplicate invoice - already submitted as processing record #{duplicate['processing_id']}.")
    elif fields.get("invoice_number") and (vendor or name):
        checks.append(_check("Duplicate detection", "PASS",
                             "No earlier invoice with this vendor and invoice number."))
    else:
        checks.append(_check("Duplicate detection", "REVIEW",
                             "Cannot check for duplicates without a vendor and an invoice number."))
        flag.append("Duplicate check could not be completed.")

    # ---- Cumulative PO amount (only APPROVED invoices count as consumed) -------
    po_balance = None
    if po_ok and total is not None and not duplicate:
        po_balance = db.get_po_balance(po["po_number"], po["po_amount"], po["tolerance_percentage"])
        consumed = Decimal(str(po_balance["consumed"]))
        po_amount = Decimal(str(po_balance["po_amount"]))
        max_allowed = Decimal(str(po_balance["max_allowed_cumulative"]))
        projected = consumed + total
        history = (f"already approved {_inr(consumed)} + this invoice {_inr(total)} = {_inr(projected)}")
        if projected <= po_amount:
            checks.append(_check("PO amount validation", "PASS",
                                 f"Cumulative {history}, within PO amount {_inr(po_amount)}."))
        elif projected <= max_allowed:
            checks.append(_check("PO amount validation", "REVIEW",
                                 f"Cumulative {history}, above PO amount {_inr(po_amount)} but within "
                                 f"{po_balance['tolerance_percentage']:g}% tolerance (limit {_inr(max_allowed)})."))
            flag.append("Cumulative amount exceeds the PO amount but is within tolerance.")
        else:
            checks.append(_check("PO amount validation", "FAIL",
                                 f"Cumulative {history}, above the tolerance limit {_inr(max_allowed)}."))
            reject.append(f"Cumulative amount {_inr(projected)} exceeds the PO tolerance limit {_inr(max_allowed)}.")
    else:
        why = ("this invoice is a duplicate" if duplicate else
               "the PO or vendor could not be validated" if po is not None or po_number else
               "there is no PO number")
        if total is None:
            why = "the invoice total is missing"
        checks.append(_check("PO amount validation", "SKIPPED", f"Not evaluated because {why}."))

    # ---- Decision (rejections outrank flags) --------------------------------
    if reject:
        decision = db.DECISION_REJECTED
        explanation = "Rejected: " + " ".join(dict.fromkeys(reject))
    elif flag:
        decision = db.DECISION_FLAGGED
        explanation = "Flagged for human review: " + " ".join(dict.fromkeys(flag))
    else:
        decision = db.DECISION_APPROVED
        explanation = ("Approved: approved vendor, valid active PO, no duplicate, totals reconcile, "
                       "and the cumulative amount is within the PO amount.")

    return {"decision": decision, "explanation": explanation, "validation_results": checks,
            "vendor": vendor, "po": po, "duplicate": duplicate, "po_balance": po_balance}


# ==========================================================================
# 3. SAVING
# ==========================================================================
def _build_record(filename, fields, evaluation):
    vendor = evaluation["vendor"]
    duplicate = evaluation["duplicate"]
    return {
        "filename": filename,
        "vendor_id": vendor["vendor_id"] if vendor else None,
        "vendor_name": vendor["vendor_name"] if vendor else fields.get("vendor_name"),
        "invoice_number": fields.get("invoice_number"),
        "invoice_date": fields.get("invoice_date"),
        "due_date": fields.get("due_date"),
        "po_number": fields.get("po_number"),
        "currency": fields.get("currency") or "INR",
        "subtotal": fields.get("subtotal"),
        "tax_amount": fields.get("tax_amount"),
        "total_amount": fields.get("total_amount"),
        "decision": evaluation["decision"],
        "decision_explanation": evaluation["explanation"],
        "duplicate_of": duplicate["processing_id"] if duplicate else None,
        "validation_results": evaluation["validation_results"],
        "extracted_fields": {k: v for k, v in fields.items() if k != "raw_text"},
    }


def save_evaluation(filename, fields, evaluation):
    """Save the finished result to the database and return its processing_id."""
    record = _build_record(filename, fields, evaluation)
    try:
        return db.save_processing_record(record)
    except db.DuplicateInvoiceError:
        # Someone saved the same invoice a split second ago: store this one as a duplicate.
        original = db.find_duplicate(record["vendor_id"], record["vendor_name"], record["invoice_number"])
        if original is None:
            raise
        record["duplicate_of"] = original["processing_id"]
        record["decision"] = db.DECISION_REJECTED
        record["decision_explanation"] = ("Rejected: Duplicate invoice - already submitted as "
                                          f"processing record #{original['processing_id']}.")
        return db.save_processing_record(record)


def process_invoice(pdf_path):
    """Extract, validate, decide and save one invoice PDF. Returns the full result."""
    path = Path(pdf_path)
    fields = extract_invoice_fields(path)
    evaluation = evaluate_invoice(fields)
    processing_id = save_evaluation(path.name, fields, evaluation)
    saved = db.get_invoice(processing_id)
    return {
        "processing_id": processing_id,
        "decision": saved["decision"],
        "explanation": saved["decision_explanation"],
        "extracted_fields": fields,
        "validation_results": saved["validation_results"],
        "record": saved,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python invoice_processor.py <invoice.pdf>   (dry run: extracts and evaluates, saves nothing)")
        raise SystemExit(1)
    f = extract_invoice_fields(sys.argv[1])
    for k, v in f.items():
        if k != "raw_text":
            print(f"{k:22} {v}")
    ev = evaluate_invoice(f)
    print("\nDECISION:", ev["decision"], "-", ev["explanation"])
    for c in ev["validation_results"]:
        print(f"  [{c['result']:7}] {c['check']}: {c['explanation']}")