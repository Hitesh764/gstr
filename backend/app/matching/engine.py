"""
The core reconciliation matching engine.

Deliberately decoupled from the database and the web framework: it takes
plain lists of "entry-like" objects (anything with the right attributes —
ORM rows, SimpleNamespace, or dicts wrapped by `as_entry`) and returns a
list of MatchResult. This lets scripts/test_matching.py exercise the exact
same logic the API uses, without a DB or HTTP server in the loop.

Matching priority (see project brief):
  1. Exact match            -> MATCHED
  2. Formatting mismatch    -> FORMATTING_MISMATCH (auto-resolved)
  3. Value mismatch         -> VALUE_MISMATCH (flagged for review)
  4. No match in GSTR-2B    -> SUPPLIER_NOT_FILED
  5. No match in books      -> MISSING_IN_BOOKS

Matching is one-to-one and greedy: once a GSTR-2B entry is consumed by a
purchase-register entry, it is removed from the candidate pool for
everything after it.
"""
import hashlib
from dataclasses import dataclass, asdict
from datetime import date
from types import SimpleNamespace
from typing import Iterable, List, Optional

from app.config import (
    EXACT_MATCH_VALUE_TOLERANCE,
    FORMATTING_VALUE_TOLERANCE,
    FORMATTING_DATE_TOLERANCE_DAYS,
    SIMILAR_INVOICE_NUMBER_RATIO,
    VALUE_MISMATCH_DATE_WINDOW_DAYS,
)
from app.money import to_float, money_str
from app.matching.normalize import (
    normalize_invoice_number,
    normalize_gstin,
    invoice_number_similarity,
)

# String constants mirror app.models.MatchStatus values exactly, but this
# module stays importable/testable without touching the DB layer.
MATCHED = "MATCHED"
FORMATTING_MISMATCH = "FORMATTING_MISMATCH"
SUPPLIER_NOT_FILED = "SUPPLIER_NOT_FILED"
VALUE_MISMATCH = "VALUE_MISMATCH"
MISSING_IN_BOOKS = "MISSING_IN_BOOKS"


@dataclass
class MatchTolerances:
    """Tolerances for one reconciliation run.

    Passed in rather than read from module-level constants, so that a firm
    can configure its own thresholds (some want tighter matching than
    others) and so a historical run stays explicable with the exact
    settings it used. `from_config()` reproduces the system defaults.
    """
    exact_value: float = EXACT_MATCH_VALUE_TOLERANCE
    formatting_value: float = FORMATTING_VALUE_TOLERANCE
    formatting_date_days: int = FORMATTING_DATE_TOLERANCE_DAYS
    similar_invoice_ratio: float = SIMILAR_INVOICE_NUMBER_RATIO
    value_mismatch_date_days: int = VALUE_MISMATCH_DATE_WINDOW_DAYS
    # Confidence at or above which a FORMATTING_MISMATCH is auto-resolved
    # instead of being queued for a human.
    auto_resolve_threshold: int = 80

    @classmethod
    def from_config(cls) -> "MatchTolerances":
        return cls()

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_TOLERANCES = MatchTolerances()


def compute_match_key(vendor_gstin: str, invoice_number: str) -> str:
    """Stable identity for a real-world invoice, independent of any run.

    Exceptions carry human work: resolution notes and approvals. When a
    reconciliation is re-run, result rows are recomputed and replaced, so
    that workflow must be re-attached to the same underlying invoice.
    Hashing the normalised vendor GSTIN and invoice number yields an
    identifier that survives both a re-run and a re-upload.
    """
    basis = f"{normalize_gstin(vendor_gstin)}|{normalize_invoice_number(invoice_number)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass
class MatchResult:
    purchase_entry_id: Optional[str]
    gstr2b_entry_id: Optional[str]
    match_status: str
    confidence_score: int          # 0-100
    notes: str
    match_key: Optional[str] = None


def as_entry(d: dict) -> SimpleNamespace:
    """Convenience: turn a plain dict into an attribute-accessible entry."""
    return SimpleNamespace(**d)


def _tv(entry) -> float:
    """Taxable value as a plain float.

    Entries arrive either as ORM rows (NUMERIC -> Decimal) or as plain
    dicts from tests and scripts (float). All tolerance arithmetic below is
    comparison-only against whole-rupee thresholds, so normalising to float
    here keeps one code path and avoids Decimal/float TypeErrors.
    """
    return to_float(entry.taxable_value)


def _date_diff_days(a: date, b: date) -> int:
    return abs((a - b).days)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pct(score_0_to_1: float) -> int:
    """Confidence is exposed as an integer percentage (0-100)."""
    return int(round(_clamp(score_0_to_1) * 100))


def _find_exact(purchase, candidates, tol: MatchTolerances):
    """Priority 1: same normalized invoice number + taxable value within tolerance."""
    p_inv = normalize_invoice_number(purchase.invoice_number)
    best, best_diff = None, None
    for c in candidates:
        if normalize_invoice_number(c.invoice_number) != p_inv:
            continue
        diff = abs(_tv(purchase) - _tv(c))
        if diff <= tol.exact_value:
            if best is None or diff < best_diff:
                best, best_diff = c, diff
    if best is None:
        return None
    confidence = _clamp(1.0 - (best_diff / max(tol.exact_value, 0.01)) * 0.05, 0.95, 1.0)
    return best, confidence, best_diff


def _find_formatting_mismatch(purchase, candidates, tol: MatchTolerances):
    """Priority 2: same GSTIN, date and value within tolerance, but the
    invoice number genuinely differs after normalization."""
    p_inv = normalize_invoice_number(purchase.invoice_number)
    best, best_score = None, None
    for c in candidates:
        if normalize_invoice_number(c.invoice_number) == p_inv:
            continue  # that is an exact-match case, not this one
        date_diff = _date_diff_days(purchase.invoice_date, c.invoice_date)
        value_diff = abs(_tv(purchase) - _tv(c))
        if date_diff > tol.formatting_date_days:
            continue
        if value_diff > tol.formatting_value:
            continue
        # composite score: closer date + closer value + more similar text = better
        score = (
            (date_diff / max(tol.formatting_date_days, 1)) * 0.4
            + (value_diff / max(tol.formatting_value, 0.01)) * 0.4
            + (1 - invoice_number_similarity(purchase.invoice_number, c.invoice_number)) * 0.2
        )
        if best is None or score < best_score:
            best, best_score = c, score
    if best is None:
        return None
    date_diff = _date_diff_days(purchase.invoice_date, best.invoice_date)
    value_diff = abs(_tv(purchase) - _tv(best))
    confidence = _clamp(
        0.95
        - (value_diff / max(tol.formatting_value, 0.01)) * 0.25
        - (date_diff / max(tol.formatting_date_days, 1)) * 0.15,
        0.55,
        0.95,
    )
    return best, confidence, (date_diff, value_diff)


def _find_value_mismatch(purchase, candidates, tol: MatchTolerances):
    """Priority 3: same/similar invoice number but the taxable value differs
    beyond tolerance -> likely a credit note or a data-entry error.

    Two passes, deliberately in this order:
      1. Same normalized invoice number (the common case: a credit note
         changes the amount, never the invoice number) — any date within
         the wider window counts.
      2. Only if no same-number candidate exists at all: a near-identical
         invoice number within a tight date window. Intentionally strict,
         because sequential numbers like 'INV-032' and 'INV-020' from one
         vendor are textually similar but unrelated, so a loose threshold
         here would manufacture false positives.
    """
    p_norm = normalize_invoice_number(purchase.invoice_number)
    same_number = [c for c in candidates if normalize_invoice_number(c.invoice_number) == p_norm]
    if same_number:
        same_number.sort(key=lambda c: _date_diff_days(purchase.invoice_date, c.invoice_date))
        best = same_number[0]
        confidence = 0.9
        value_diff = _tv(purchase) - _tv(best)
        return best, confidence, value_diff

    best, best_sim = None, None
    for c in candidates:
        date_diff = _date_diff_days(purchase.invoice_date, c.invoice_date)
        if date_diff > 2:
            continue
        sim = invoice_number_similarity(purchase.invoice_number, c.invoice_number)
        if sim < tol.similar_invoice_ratio:
            continue
        if best is None or sim > best_sim:
            best, best_sim = c, sim
    if best is None:
        return None
    confidence = _clamp(best_sim * 0.8, 0.4, 0.8)
    value_diff = _tv(purchase) - _tv(best)
    return best, confidence, value_diff


def _explain(status: str, purchase=None, gstr=None, extra=None) -> str:
    if status == MATCHED:
        return (
            f"Vendor {purchase.vendor_name} invoice {purchase.invoice_number} "
            f"matched GSTR-2B exactly (taxable value ₹{money_str(purchase.taxable_value)})."
        )
    if status == FORMATTING_MISMATCH:
        return (
            f"Invoice number formatting differs ('{purchase.invoice_number}' in books vs "
            f"'{gstr.invoice_number}' in GSTR-2B) but vendor, date and value align — "
            f"treated as the same invoice from {purchase.vendor_name}. Auto-resolved."
        )
    if status == VALUE_MISMATCH:
        diff = _tv(purchase) - _tv(gstr)
        if diff > 0:
            return (
                f"Vendor {purchase.vendor_name} invoice appears in GSTR-2B with taxable "
                f"value ₹{money_str(gstr.taxable_value)} but books show "
                f"₹{money_str(purchase.taxable_value)} — possible unrecorded credit note of "
                f"₹{diff:,.2f}, or a data entry error. Review before filing."
            )
        return (
            f"Vendor {purchase.vendor_name} invoice appears in GSTR-2B with taxable "
            f"value ₹{money_str(gstr.taxable_value)}, higher than the "
            f"₹{money_str(purchase.taxable_value)} recorded in books — check for an "
            f"amendment or a missed debit note. Review before filing."
        )
    if status == SUPPLIER_NOT_FILED:
        return (
            f"Vendor {purchase.vendor_name} invoice {purchase.invoice_number} "
            f"(₹{money_str(purchase.taxable_value)} taxable) was not found in GSTR-2B — the "
            f"supplier has likely not filed their GSTR-1 for this period yet. ITC is not "
            f"eligible until it appears."
        )
    if status == MISSING_IN_BOOKS:
        return (
            f"GSTR-2B shows invoice {gstr.invoice_number} from vendor "
            f"{gstr.vendor_name or gstr.vendor_gstin} (₹{money_str(gstr.taxable_value)} taxable) "
            f"that is not recorded in the purchase register — check whether this purchase was "
            f"missed in the books."
        )
    return ""


def reconcile(
    purchase_entries: Iterable,
    gstr2b_entries: Iterable,
    tolerances: Optional[MatchTolerances] = None,
) -> List[MatchResult]:
    """Run the full reconciliation.

    Returns one MatchResult per purchase-register entry, plus one per
    GSTR-2B entry that no purchase entry claimed.
    """
    tol = tolerances or DEFAULT_TOLERANCES
    purchase_entries = list(purchase_entries)
    gstr2b_entries = list(gstr2b_entries)

    # Bucket GSTR-2B entries by normalized vendor GSTIN, and track which are
    # still available for matching (greedy one-to-one consumption).
    by_gstin: dict[str, list] = {}
    for g in gstr2b_entries:
        by_gstin.setdefault(normalize_gstin(g.vendor_gstin), []).append(g)
    consumed_ids: set = set()

    results: List[MatchResult] = []

    # Deterministic order, so a re-run over unchanged data produces
    # identical output.
    for purchase in sorted(
        purchase_entries,
        key=lambda p: (p.vendor_gstin, p.invoice_date, p.invoice_number),
    ):
        candidates = [
            g for g in by_gstin.get(normalize_gstin(purchase.vendor_gstin), [])
            if g.id not in consumed_ids
        ]
        key = compute_match_key(purchase.vendor_gstin, purchase.invoice_number)

        hit = _find_exact(purchase, candidates, tol)
        if hit:
            gstr, confidence, _ = hit
            consumed_ids.add(gstr.id)
            results.append(MatchResult(
                purchase_entry_id=purchase.id, gstr2b_entry_id=gstr.id,
                match_status=MATCHED, confidence_score=_pct(confidence),
                notes=_explain(MATCHED, purchase=purchase, gstr=gstr),
                match_key=key,
            ))
            continue

        hit = _find_formatting_mismatch(purchase, candidates, tol)
        if hit:
            gstr, confidence, _ = hit
            consumed_ids.add(gstr.id)
            results.append(MatchResult(
                purchase_entry_id=purchase.id, gstr2b_entry_id=gstr.id,
                match_status=FORMATTING_MISMATCH, confidence_score=_pct(confidence),
                notes=_explain(FORMATTING_MISMATCH, purchase=purchase, gstr=gstr),
                match_key=key,
            ))
            continue

        hit = _find_value_mismatch(purchase, candidates, tol)
        if hit:
            gstr, confidence, _ = hit
            consumed_ids.add(gstr.id)
            results.append(MatchResult(
                purchase_entry_id=purchase.id, gstr2b_entry_id=gstr.id,
                match_status=VALUE_MISMATCH, confidence_score=_pct(confidence),
                notes=_explain(VALUE_MISMATCH, purchase=purchase, gstr=gstr),
                match_key=key,
            ))
            continue

        # Priority 4: nothing in GSTR-2B for this invoice at all.
        results.append(MatchResult(
            purchase_entry_id=purchase.id, gstr2b_entry_id=None,
            match_status=SUPPLIER_NOT_FILED, confidence_score=0,
            notes=_explain(SUPPLIER_NOT_FILED, purchase=purchase),
            match_key=key,
        ))

    # Priority 5: anything left in GSTR-2B was never claimed by a purchase entry.
    for gstr in gstr2b_entries:
        if gstr.id in consumed_ids:
            continue
        results.append(MatchResult(
            purchase_entry_id=None, gstr2b_entry_id=gstr.id,
            match_status=MISSING_IN_BOOKS, confidence_score=0,
            notes=_explain(MISSING_IN_BOOKS, gstr=gstr),
            match_key=compute_match_key(gstr.vendor_gstin, gstr.invoice_number),
        ))

    return results
