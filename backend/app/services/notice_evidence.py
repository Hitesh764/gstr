"""
Assemble the client's own reconciliation records as evidence for a notice.

This is the "real data only" half of drafting a response. The drafter is never
asked to recall or estimate anything about the client: it is handed a numbered
list of facts taken from the database, and told to cite them. Building that
list is plain, deterministic code here, so what the drafter can see is exactly
what a person can inspect on screen (`GET /api/notices/{id}/evidence`).

Everything is read through the TenantSession, so a notice can only ever pull
evidence from its own firm's records.
"""
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import joinedload

from app import models
from app.config import settings
from app.money import money_str
from app.notice_ai.base import EvidenceItem, EvidencePack
from app.tenancy import TenantSession

MAX_PERIODS = 24    # a two-year notice is the longest we will assemble by month

MATCH_LABEL = {
    models.MatchStatus.MATCHED: "matched the supplier's return",
    models.MatchStatus.FORMATTING_MISMATCH: "matched apart from formatting differences",
    models.MatchStatus.SUPPLIER_NOT_FILED: "not found in GSTR-2B (supplier has not reported it)",
    models.MatchStatus.VALUE_MISMATCH: "found in GSTR-2B with a different value",
    models.MatchStatus.CREDIT_NOTE_DISCREPANCY: "credit note discrepancy",
    models.MatchStatus.MISSING_IN_BOOKS: "in GSTR-2B but not in the purchase register",
}

# Exceptions in these states are settled and no longer count as credit at risk.
_SETTLED = (models.ExceptionStatus.APPROVED, models.ExceptionStatus.AUTO_RESOLVED)


def months_between(start: str, end: str) -> list[str]:
    """Inclusive list of YYYY-MM from `start` to `end`, capped at MAX_PERIODS."""
    y, m = int(start[:4]), int(start[5:])
    ey, em = int(end[:4]), int(end[5:])
    out: list[str] = []
    while (y, m) <= (ey, em) and len(out) < MAX_PERIODS:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def notice_periods(notice: models.Notice) -> list[str]:
    if not notice.period:
        return []
    end = notice.period_to or notice.period
    if end < notice.period:
        return []
    return months_between(notice.period, end)


def _tax(entry) -> Decimal:
    if entry is None:
        return Decimal("0")
    return (entry.cgst or Decimal("0")) + (entry.sgst or Decimal("0")) + (entry.igst or Decimal("0"))


def _entry_for(result: models.ReconciliationResult):
    return result.purchase_entry or result.gstr2b_entry


def build_evidence(tenant: TenantSession, notice: models.Notice) -> EvidencePack:
    pack = EvidencePack()

    if not notice.client_gstin_id:
        pack.gaps.append("No GSTIN is linked to this notice, so no reconciliation data was consulted.")
        return pack
    gstin = tenant.get(models.ClientGSTIN, notice.client_gstin_id)
    if gstin is None:
        pack.gaps.append("The GSTIN linked to this notice could not be found.")
        return pack
    pack.gstin = gstin.gstin

    periods = notice_periods(notice)
    if not periods:
        pack.gaps.append("The notice has no tax period set, so no reconciliation data was consulted.")
        return pack
    pack.periods = periods

    summaries: list[EvidenceItem] = []
    flagged: list[tuple[Decimal, models.ReconciliationResult, models.ReconciliationRun]] = []
    counter = 0

    for period in periods:
        run = (
            tenant.query(models.ReconciliationRun)
            .filter(models.ReconciliationRun.client_gstin_id == gstin.id,
                    models.ReconciliationRun.period == period,
                    models.ReconciliationRun.is_current.is_(True),
                    models.ReconciliationRun.status == models.RunStatus.COMPLETED)
            .first()
        )
        if run is None:
            pack.gaps.append(f"No completed reconciliation exists for {period}.")
            continue

        results = (
            tenant.query(models.ReconciliationResult)
            .options(joinedload(models.ReconciliationResult.purchase_entry),
                     joinedload(models.ReconciliationResult.gstr2b_entry),
                     joinedload(models.ReconciliationResult.exception))
            .filter(models.ReconciliationResult.run_id == run.id)
            .all()
        )

        counts: dict[models.MatchStatus, int] = {}
        at_risk = Decimal("0")
        for r in results:
            counts[r.match_status] = counts.get(r.match_status, 0) + 1
            if r.match_status == models.MatchStatus.MATCHED:
                continue
            exc = r.exception
            settled = exc is not None and exc.status in _SETTLED
            if not settled:
                at_risk += _tax(_entry_for(r))
            if exc is not None:
                flagged.append((_tax(_entry_for(r)), r, run))

        breakdown = "; ".join(
            f"{n} {MATCH_LABEL[s]}" for s, n in sorted(counts.items(), key=lambda kv: kv[0].value)
        ) or "no invoices"
        counter += 1
        completed = f", completed {run.completed_at:%d-%m-%Y}" if run.completed_at else ""
        summaries.append(EvidenceItem(
            id=f"E{counter}", kind="RUN_SUMMARY",
            text=(f"For {period} (reconciliation run version {run.version}{completed}): "
                  f"{len(results)} invoices were compared: {breakdown}. Tax on flagged invoices "
                  f"not yet settled: Rs {money_str(at_risk)}."),
            data={"period": period, "run_id": run.id, "run_version": run.version,
                  "invoices": len(results), "unsettled_tax": str(at_risk),
                  "counts": {s.value: n for s, n in counts.items()}},
        ))

    # Largest tax first, so if the list has to be cut, what is dropped is small.
    flagged.sort(key=lambda t: t[0], reverse=True)
    room = max(0, settings.notice_evidence_max_items - len(summaries))
    if len(flagged) > room:
        pack.gaps.append(
            f"Only the {room} flagged invoices with the highest tax are listed, out of {len(flagged)}."
        )
    items = list(summaries)
    for tax, result, run in flagged[:room]:
        counter += 1
        items.append(_exception_item(f"E{counter}", tax, result, run))
    pack.items = items
    return pack


def _exception_item(item_id: str, tax: Decimal, result: models.ReconciliationResult,
                    run: models.ReconciliationRun) -> EvidenceItem:
    entry = _entry_for(result)
    exc = result.exception
    inv_date = entry.invoice_date.strftime("%d-%m-%Y") if entry and entry.invoice_date else "unknown date"
    taxable = entry.taxable_value if entry is not None else Decimal("0")

    handling = f"Handling status: {exc.status.value.replace('_', ' ').lower()}."
    if exc.resolution_notes:
        who = "Approved explanation" if exc.status == models.ExceptionStatus.APPROVED \
            else "Proposed explanation, not yet approved"
        handling += f" {who}: {exc.resolution_notes.strip()}"

    note = f" {result.plain_language_note.strip()}" if result.plain_language_note else ""
    text = (
        f"Invoice {entry.invoice_number if entry else '?'} dated {inv_date} from "
        f"{(entry.vendor_name or 'an unnamed vendor') if entry else '?'} "
        f"(GSTIN {entry.vendor_gstin if entry else '?'}), period {run.period}: taxable value "
        f"Rs {money_str(taxable)}, tax Rs {money_str(tax)}. Reconciliation finding: "
        f"{MATCH_LABEL.get(result.match_status, result.match_status.value)}.{note} {handling}"
    )
    return EvidenceItem(
        id=item_id, kind="EXCEPTION", text=text,
        data={"result_id": result.id, "exception_id": exc.id if exc else None,
              "period": run.period,
              "invoice_number": entry.invoice_number if entry else None,
              "vendor_gstin": entry.vendor_gstin if entry else None,
              "taxable_value": str(taxable), "tax": str(tax),
              "match_status": result.match_status.value,
              "exception_status": exc.status.value if exc else None},
    )
