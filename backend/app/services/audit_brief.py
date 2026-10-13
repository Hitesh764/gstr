"""
The pre-audit brief: a client's GST compliance health, before a partner opens the file.

Scope, deliberately: everything here is computed from data this platform
already holds — purchase-register invoices, GSTR-2B, and the reconciliation
and exception history built from them. It does **not** attempt ratio analysis
(inventory turnover, revenue trends), related-party detection, or cash-payment
flags, because none of that data exists here: there is no general ledger,
trial balance or chart of accounts in this schema, only an accounts-payable
subledger scoped to GST input tax credit. A brief that invented those figures
from data it does not have would be worse than not having the brief at all.

What this *can* honestly tell a partner, from months of reconciliation
history:

* Is unresolved GST risk (ITC at risk) growing, shrinking, or steady?
* Is one vendor an outsized share of this client's claimed credit?
* Which vendors keep causing the same problem, month after month?
* How much of the last N months has actually been reconciled at all?
* Is anything sitting open long enough that it should worry someone?

Each finding traces back to real rows — a run, an exception, an invoice — so
"this looks concentrated" is never asserted without the number behind it.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app import models
from app.money import ZERO, to_decimal
from app.services.portfolio import UNSETTLED_EXCEPTION_STATUSES
from app.services.reconciliation_service import AT_RISK_STATUSES
from app.tenancy import TenantSession

# A single vendor claiming more than this share of a client's ITC is called
# out as a concentration risk. Below this, uneven vendor spend is normal.
CONCENTRATION_THRESHOLD = 0.30
# A vendor flagged as an exception in at least this many of the lookback
# periods is a pattern, not a one-off.
RECURRING_MIN_PERIODS = 2
# An open exception older than this is called out by name in the brief.
STALE_EXCEPTION_DAYS = 60
# Three periods moving the same (worse) direction is treated as a trend.
TREND_MIN_POINTS = 3


def _period_before(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y-1:04d}-12" if m == 1 else f"{y:04d}-{m-1:02d}"


def periods_ending(as_of: str, n: int) -> list[str]:
    """The `n` periods up to and including `as_of`, oldest first."""
    out = [as_of]
    for _ in range(n - 1):
        out.append(_period_before(out[-1]))
    return list(reversed(out))


@dataclass
class PeriodPoint:
    period: str
    reconciled: bool
    invoice_count: int = 0
    matched_count: int = 0
    itc_claimed: Decimal = field(default_factory=lambda: ZERO)
    itc_eligible: Decimal = field(default_factory=lambda: ZERO)
    itc_blocked: Decimal = field(default_factory=lambda: ZERO)
    itc_at_risk: Decimal = field(default_factory=lambda: ZERO)


@dataclass
class VendorShare:
    vendor_gstin: str
    vendor_name: Optional[str]
    itc_claimed: Decimal
    share: float


@dataclass
class RecurringVendor:
    vendor_gstin: str
    vendor_name: Optional[str]
    periods_flagged: list[str]
    statuses: list[str]


@dataclass
class StaleException:
    exception_id: str
    period: str
    status: str
    age_days: int
    vendor_name: Optional[str]
    invoice_number: Optional[str]


@dataclass
class RedFlag:
    severity: str          # "critical" | "warning"
    message: str


@dataclass
class AuditBrief:
    client_id: str
    client_name: str
    as_of: str
    lookback_periods: list[str]
    points: list[PeriodPoint]
    filing_consistency: float          # fraction of window periods reconciled
    reconciled_periods: int
    total_periods: int
    latest_reconciled_period: Optional[str]
    latest_itc_at_risk: Decimal
    vendor_concentration: list[VendorShare]
    recurring_vendors: list[RecurringVendor]
    stale_exceptions: list[StaleException]
    open_exception_count: int
    red_flags: list[RedFlag]
    generated_at: datetime


def _entry_tax(entry) -> Decimal:
    return to_decimal(entry.cgst) + to_decimal(entry.sgst) + to_decimal(entry.igst)


def _current_runs(tenant: TenantSession, gstin_ids: list[str], periods: list[str]):
    if not gstin_ids:
        return []
    return (
        tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id.in_(gstin_ids),
                models.ReconciliationRun.period.in_(periods),
                models.ReconciliationRun.is_current.is_(True),
                models.ReconciliationRun.status == models.RunStatus.COMPLETED)
        .all()
    )


def build_audit_brief(
    tenant: TenantSession, client: models.Client, *,
    as_of: Optional[str] = None, lookback: int = 12,
) -> AuditBrief:
    lookback = max(1, min(lookback, 36))
    gstins = (
        tenant.query(models.ClientGSTIN)
        .filter(models.ClientGSTIN.client_id == client.id)
        .all()
    )
    gstin_ids = [g.id for g in gstins]

    if as_of is None:
        latest = (
            tenant.query(models.ReconciliationRun)
            .filter(models.ReconciliationRun.client_gstin_id.in_(gstin_ids),
                    models.ReconciliationRun.is_current.is_(True))
            .order_by(models.ReconciliationRun.period.desc())
            .first()
            if gstin_ids else None
        )
        from app.services.gstr2b_sync import previous_period
        as_of = latest.period if latest else previous_period()

    periods = periods_ending(as_of, lookback)
    runs = _current_runs(tenant, gstin_ids, periods)
    run_ids = [r.id for r in runs]
    runs_by_period: dict[str, list] = {}
    for r in runs:
        runs_by_period.setdefault(r.period, []).append(r)

    results = (
        tenant.query(models.ReconciliationResult)
        .filter(models.ReconciliationResult.run_id.in_(run_ids))
        .all()
        if run_ids else []
    )
    results_by_run: dict[str, list] = {}
    for r in results:
        results_by_run.setdefault(r.run_id, []).append(r)

    exceptions = (
        tenant.query(models.ReconciliationException)
        .filter(models.ReconciliationException.client_gstin_id.in_(gstin_ids),
                models.ReconciliationException.period.in_(periods))
        .all()
        if gstin_ids else []
    )
    exceptions_by_result: dict[str, models.ReconciliationException] = {
        e.reconciliation_result_id: e for e in exceptions
    }

    points: list[PeriodPoint] = []
    vendor_totals: dict[str, Decimal] = {}
    vendor_names: dict[str, str] = {}
    vendor_flag_periods: dict[str, set] = {}
    vendor_flag_statuses: dict[str, set] = {}

    for period in periods:
        period_runs = runs_by_period.get(period, [])
        point = PeriodPoint(period=period, reconciled=bool(period_runs))
        for run in period_runs:
            for r in results_by_run.get(run.id, []):
                status_val = r.match_status.value if hasattr(r.match_status, "value") else r.match_status
                if r.purchase_entry_id and r.purchase_entry:
                    point.invoice_count += 1
                    if status_val == "MATCHED":
                        point.matched_count += 1
                    tax = _entry_tax(r.purchase_entry)
                    point.itc_claimed += tax
                    vendor_gstin = r.purchase_entry.vendor_gstin
                    vendor_totals[vendor_gstin] = vendor_totals.get(vendor_gstin, ZERO) + tax
                    if r.purchase_entry.vendor_name:
                        vendor_names[vendor_gstin] = r.purchase_entry.vendor_name

                    exc = exceptions_by_result.get(r.id)
                    unsettled = exc is not None and exc.status in UNSETTLED_EXCEPTION_STATUSES
                    if status_val in AT_RISK_STATUSES and unsettled:
                        point.itc_at_risk += tax
                        vendor_flag_periods.setdefault(vendor_gstin, set()).add(period)
                        vendor_flag_statuses.setdefault(vendor_gstin, set()).add(status_val)
                if r.gstr2b_entry_id and r.gstr2b_entry:
                    if r.gstr2b_entry.itc_available:
                        point.itc_eligible += _entry_tax(r.gstr2b_entry)
                    else:
                        point.itc_blocked += _entry_tax(r.gstr2b_entry)
        points.append(point)

    reconciled_periods = sum(1 for p in points if p.reconciled)
    filing_consistency = reconciled_periods / len(periods) if periods else 0.0

    total_claimed = sum(vendor_totals.values(), ZERO)
    concentration = []
    for gstin, amount in sorted(vendor_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        share = float(amount / total_claimed) if total_claimed else 0.0
        concentration.append(VendorShare(
            vendor_gstin=gstin, vendor_name=vendor_names.get(gstin),
            itc_claimed=amount, share=share,
        ))

    recurring = [
        RecurringVendor(
            vendor_gstin=gstin, vendor_name=vendor_names.get(gstin),
            periods_flagged=sorted(flagged_periods),
            statuses=sorted(vendor_flag_statuses.get(gstin, set())),
        )
        for gstin, flagged_periods in vendor_flag_periods.items()
        if len(flagged_periods) >= RECURRING_MIN_PERIODS
    ]
    recurring.sort(key=lambda v: len(v.periods_flagged), reverse=True)

    # Open work outstanding right now, not scoped to the lookback window: an
    # item opened 13 months ago is exactly the kind of thing a brief exists
    # to surface before an audit, not to age out of.
    open_now = (
        tenant.query(models.ReconciliationException)
        .filter(models.ReconciliationException.client_gstin_id.in_(gstin_ids),
                models.ReconciliationException.status.in_(UNSETTLED_EXCEPTION_STATUSES))
        .all()
        if gstin_ids else []
    )
    today = datetime.utcnow()
    stale = []
    for e in open_now:
        age = (today - e.created_at).days
        if age < STALE_EXCEPTION_DAYS:
            continue
        result = tenant.get(models.ReconciliationResult, e.reconciliation_result_id)
        entry = result.purchase_entry if result else None
        stale.append(StaleException(
            exception_id=e.id, period=e.period, status=e.status.value, age_days=age,
            vendor_name=entry.vendor_name if entry else None,
            invoice_number=entry.invoice_number if entry else None,
        ))
    stale.sort(key=lambda s: s.age_days, reverse=True)

    red_flags = _build_red_flags(
        points=points, filing_consistency=filing_consistency, concentration=concentration,
        recurring=recurring, stale=stale, total_blocked=sum((p.itc_blocked for p in points), ZERO),
        total_periods=len(periods),
    )

    latest_reconciled = next((p for p in reversed(points) if p.reconciled), None)

    return AuditBrief(
        client_id=client.id, client_name=client.name, as_of=as_of, lookback_periods=periods,
        points=points, filing_consistency=filing_consistency,
        reconciled_periods=reconciled_periods, total_periods=len(periods),
        latest_reconciled_period=latest_reconciled.period if latest_reconciled else None,
        latest_itc_at_risk=latest_reconciled.itc_at_risk if latest_reconciled else ZERO,
        vendor_concentration=concentration, recurring_vendors=recurring,
        stale_exceptions=stale, open_exception_count=len(open_now),
        red_flags=red_flags, generated_at=today,
    )


def _build_red_flags(
    *, points: list[PeriodPoint], filing_consistency: float, concentration: list[VendorShare],
    recurring: list[RecurringVendor], stale: list[StaleException], total_blocked: Decimal,
    total_periods: int,
) -> list[RedFlag]:
    flags: list[RedFlag] = []

    reconciled_points = [p for p in points if p.reconciled]
    if len(reconciled_points) >= TREND_MIN_POINTS:
        tail = reconciled_points[-TREND_MIN_POINTS:]
        if all(b.itc_at_risk > a.itc_at_risk for a, b in zip(tail, tail[1:])):
            flags.append(RedFlag(
                "critical",
                f"ITC at risk has risen for {TREND_MIN_POINTS} consecutive reconciled "
                f"periods, from {tail[0].period} to {tail[-1].period}.",
            ))

    if concentration and concentration[0].share >= CONCENTRATION_THRESHOLD:
        top = concentration[0]
        flags.append(RedFlag(
            "warning",
            f"{top.vendor_name or top.vendor_gstin} accounts for "
            f"{top.share * 100:.0f}% of ITC claimed in this window — a concentration risk "
            f"if that supplier stops filing or is disallowed.",
        ))

    for v in recurring[:5]:
        flags.append(RedFlag(
            "warning",
            f"{v.vendor_name or v.vendor_gstin} was flagged in {len(v.periods_flagged)} of "
            f"the last {total_periods} months reviewed ({', '.join(v.periods_flagged)}) — "
            f"a recurring pattern, not a one-off.",
        ))

    if filing_consistency < 1.0:
        missed = round((1 - filing_consistency) * 100)
        flags.append(RedFlag(
            "critical" if filing_consistency < 0.5 else "warning",
            f"{missed}% of the lookback window has no completed reconciliation at all.",
        ))

    if stale:
        oldest = stale[0]
        flags.append(RedFlag(
            "critical" if oldest.age_days > 180 else "warning",
            f"{len(stale)} exception{'s' if len(stale) != 1 else ''} open for more than "
            f"{STALE_EXCEPTION_DAYS} days; the oldest is {oldest.age_days} days "
            f"(period {oldest.period}).",
        ))

    if total_blocked > 0:
        flags.append(RedFlag(
            "warning",
            f"Rs {total_blocked:,.2f} of GSTR-2B credit in this window is marked not "
            f"eligible (blocked under Section 17(5)) and must not be claimed.",
        ))

    order = {"critical": 0, "warning": 1}
    flags.sort(key=lambda f: order[f.severity])
    return flags
