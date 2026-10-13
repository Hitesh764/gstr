"""
GSTN's return JSON, translated into the platform's own shapes.

GSTR-2B is a statutory document with a fixed structure that every GSP passes
through largely untouched, so this translation lives beside the adapters
rather than inside one of them. A ClearTax or MasterGST adapter reuses it.

The shape being read (field names as documented by Sandbox for GSTR-2B):

    <envelope>.data.data.data                      the GSTN payload
        .gstin   .rtnprd ("MMYYYY")   .gendt ("DD-MM-YYYY")
        .docdata.b2b[]                             one block per supplier
              .ctin  .trdnm  .supfildt  .supprd
              .inv[]                               one entry per invoice
                    .inum .dt .val .txval .igst .cgst .sgst .cess
                    .itcavl ("Y"/"N") .typ .rev .rsn
        .docdata.cdnr[]                            credit and debit notes
              .ctin .trdnm
              .nt[]   .ntnum .typ ("C"/"D") .dt .val .txval .igst .cgst .sgst

Nothing here performs I/O, so the whole translation is testable against
literal payloads.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from app.gsp.base import GSPResponseError


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------

def parse_gstn_date(raw: Any) -> Optional[date]:
    """GSTN writes dates as DD-MM-YYYY. Returns None for anything unparseable
    rather than guessing, since a wrong date silently breaks matching."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def period_to_mmyyyy(period: str) -> str:
    """'2024-11' -> '112024'."""
    year, month = period.split("-")
    return f"{int(month):02d}{int(year):04d}"


def mmyyyy_to_period(value: str) -> Optional[str]:
    """'112024' -> '2024-11'."""
    text = str(value or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    return f"{text[2:]}-{text[:2]}"


def financial_year_start(period: str) -> int:
    """The calendar year in which the financial year containing `period`
    began. India's year runs April to March: Feb 2025 belongs to FY 2024."""
    year, month = (int(x) for x in period.split("-"))
    return year if month >= 4 else year - 1


def _num(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


# ---------------------------------------------------------------------------
# Locating the payload
# ---------------------------------------------------------------------------

def find_gstn_payload(body: dict) -> dict:
    """Walk the nested `data` envelopes down to the object holding `docdata`.

    Sandbox wraps GSTN's own response, which is itself wrapped, so the payload
    sits three `data` levels deep. Walking until `docdata` appears, instead of
    hardcoding the depth, survives a provider adding or removing a wrapper.
    """
    node: Any = body
    for _ in range(6):
        if not isinstance(node, dict):
            break
        if "docdata" in node:
            return node
        node = node.get("data")
    raise GSPResponseError("The GSTR-2B response did not contain the expected 'docdata' section.")


def _tax_from_items(inv: dict) -> dict[str, Decimal]:
    """Older or alternate layouts carry tax per line item instead of on the
    invoice. Sum the lines so either shape yields the same totals."""
    totals = {"txval": Decimal("0"), "igst": Decimal("0"), "cgst": Decimal("0"),
              "sgst": Decimal("0"), "cess": Decimal("0")}
    for item in inv.get("items") or inv.get("itms") or []:
        det = item.get("itm_det", item)
        totals["txval"] += _num(det.get("txval"))
        totals["igst"] += _num(det.get("igst", det.get("iamt")))
        totals["cgst"] += _num(det.get("cgst", det.get("camt")))
        totals["sgst"] += _num(det.get("sgst", det.get("samt")))
        totals["cess"] += _num(det.get("cess", det.get("csamt")))
    return totals


def _amounts(doc: dict) -> dict[str, Decimal]:
    """Invoice-level figures when present, falling back to summed line items."""
    if any(k in doc for k in ("txval", "igst", "cgst", "sgst")):
        return {
            "txval": _num(doc.get("txval")), "igst": _num(doc.get("igst")),
            "cgst": _num(doc.get("cgst")), "sgst": _num(doc.get("sgst")),
            "cess": _num(doc.get("cess")),
        }
    return _tax_from_items(doc)


# ---------------------------------------------------------------------------
# GSTR-2B
# ---------------------------------------------------------------------------

def normalize_gstr2b(
    pages: Iterable[dict], *, gstin: str, period: str,
) -> tuple[dict, int, int, list[str]]:
    """Merge one or more response pages into the platform's GSTR-2B document.

    Returns (document, invoice_count, credit_note_count, warnings).

    Raises GSPResponseError if the statement is for a different GSTIN or period
    than was requested. A wrong-taxpayer statement would reconcile against
    nothing and look like a mass supplier failure, so it is refused here just
    as the manual upload path refuses it.
    """
    invoices: list[dict] = []
    notes: list[dict] = []
    warnings: list[str] = []
    generated_on: Optional[date] = None
    seen_any = False

    for page in pages:
        payload = find_gstn_payload(page)
        seen_any = True

        got_gstin = str(payload.get("gstin") or "").strip().upper()
        if got_gstin and got_gstin != gstin.upper():
            raise GSPResponseError(
                f"The provider returned a GSTR-2B for GSTIN {got_gstin}, but {gstin} was requested."
            )
        got_period = mmyyyy_to_period(payload.get("rtnprd"))
        if got_period and got_period != period:
            raise GSPResponseError(
                f"The provider returned GSTR-2B for {got_period}, but {period} was requested."
            )
        generated_on = generated_on or parse_gstn_date(payload.get("gendt"))

        docdata = payload.get("docdata") or {}

        for block in docdata.get("b2b") or []:
            ctin = str(block.get("ctin") or "").strip().upper()
            supplier = block.get("trdnm")
            filed_on = parse_gstn_date(block.get("supfildt"))
            for inv in block.get("inv") or []:
                number = str(inv.get("inum") or "").strip()
                inv_date = parse_gstn_date(inv.get("dt"))
                if not (ctin and number and inv_date):
                    warnings.append(
                        f"Skipped an invoice from supplier {ctin or '(unknown)'}: "
                        f"missing GSTIN, number or a readable date "
                        f"(number={number or '?'}, date={inv.get('dt')!r})."
                    )
                    continue
                amt = _amounts(inv)
                invoices.append({
                    "vendor_name": supplier,
                    "vendor_gstin": ctin,
                    "invoice_number": number,
                    "invoice_date": inv_date.isoformat(),
                    "taxable_value": _money(amt["txval"]),
                    "cgst": _money(amt["cgst"]),
                    "sgst": _money(amt["sgst"]),
                    "igst": _money(amt["igst"]),
                    "total_value": _money(_num(inv.get("val"))),
                    # "N" means the credit is blocked (for example under
                    # section 17(5)) even though the supplier reported it.
                    "itc_available": str(inv.get("itcavl", "Y")).strip().upper() != "N",
                    "filing_status": "FILED",
                    "invoice_type": inv.get("typ"),
                    "reverse_charge": str(inv.get("rev", "N")).strip().upper() == "Y",
                    "supplier_filed_on": filed_on.isoformat() if filed_on else None,
                })

        for block in docdata.get("cdnr") or []:
            ctin = str(block.get("ctin") or "").strip().upper()
            for nt in block.get("nt") or []:
                note_date = parse_gstn_date(nt.get("dt"))
                amt = _amounts(nt)
                notes.append({
                    "vendor_gstin": ctin,
                    "vendor_name": block.get("trdnm"),
                    "note_number": str(nt.get("ntnum") or "").strip(),
                    "note_type": nt.get("typ"),          # "C" credit, "D" debit
                    "note_date": note_date.isoformat() if note_date else None,
                    "taxable_value": _money(amt["txval"]),
                    "cgst": _money(amt["cgst"]),
                    "sgst": _money(amt["sgst"]),
                    "igst": _money(amt["igst"]),
                    "total_value": _money(_num(nt.get("val"))),
                    "itc_available": str(nt.get("itcavl", "Y")).strip().upper() != "N",
                })

    if not seen_any:
        raise GSPResponseError("The provider returned no GSTR-2B data.")

    document = {
        "gstin": gstin.upper(),
        "period": period,
        "generated_on": generated_on.isoformat() if generated_on else None,
        "invoices": invoices,
        "credit_notes": notes,
    }
    return document, len(invoices), len(notes), warnings


# ---------------------------------------------------------------------------
# GSTR-1 (outward supplies)
# ---------------------------------------------------------------------------

def normalize_gstr1_b2b(body: dict, *, gstin: str, period: str) -> list[dict]:
    """The taxpayer's own B2B sales, from GSTR-1.

    Field names differ from GSTR-2B: recipients are `ctin`, dates are `idt`,
    and tax sits inside `itms[].itm_det` as iamt/camt/samt.
    """
    node: Any = body
    b2b = None
    for _ in range(6):
        if isinstance(node, dict) and "b2b" in node:
            b2b = node["b2b"]
            break
        node = node.get("data") if isinstance(node, dict) else None
    if b2b is None:
        raise GSPResponseError("The GSTR-1 response did not contain a 'b2b' section.")

    out: list[dict] = []
    for block in b2b:
        ctin = str(block.get("ctin") or "").strip().upper()
        for inv in block.get("inv") or []:
            amt = _tax_from_items(inv)
            inv_date = parse_gstn_date(inv.get("idt"))
            out.append({
                "customer_gstin": ctin,
                "invoice_number": str(inv.get("inum") or "").strip(),
                "invoice_date": inv_date.isoformat() if inv_date else None,
                "taxable_value": _money(amt["txval"]),
                "cgst": _money(amt["cgst"]),
                "sgst": _money(amt["sgst"]),
                "igst": _money(amt["igst"]),
                "total_value": _money(_num(inv.get("val"))),
            })
    return out
