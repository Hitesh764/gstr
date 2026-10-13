"""Deterministic ageing calculations over dated, open ledger items."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

BUCKETS = ("0-30", "31-60", "61-90", "91-180", "180+")


def bucket_for_age(age_days: int) -> str:
    if age_days <= 30:
        return "0-30"
    if age_days <= 60:
        return "31-60"
    if age_days <= 90:
        return "61-90"
    if age_days <= 180:
        return "91-180"
    return "180+"


def build_ageing_data(lines, *, as_of_date: date, account_type: str) -> dict:
    """Age by due date, falling back to invoice date, and preserve line provenance."""
    totals = {bucket: Decimal("0.00") for bucket in BUCKETS}
    parties = defaultdict(lambda: {
        "buckets": {bucket: Decimal("0.00") for bucket in BUCKETS}, "items": []
    })
    oldest_reviews = []
    for line in lines:
        if line.account_type.value != account_type:
            continue
        basis = line.due_date or line.invoice_date
        raw_age_days = (as_of_date - basis).days
        age_days = max(0, raw_age_days)
        bucket = bucket_for_age(age_days)
        amount = Decimal(line.open_balance)
        totals[bucket] += amount
        party = parties[line.party_name]
        party["buckets"][bucket] += amount
        item = {
            "ledger_line_id": line.id,
            "invoice_number": line.invoice_number,
            "invoice_date": line.invoice_date.isoformat(),
            "due_date": line.due_date.isoformat() if line.due_date else None,
            "ageing_date_basis": "DUE_DATE" if line.due_date else "INVOICE_DATE_FALLBACK",
            "age_days": age_days,
            "not_yet_due": raw_age_days < 0,
            "open_balance": str(amount),
            "bucket": bucket,
            "explanation": line.explanation,
            "explanation_on_file": bool((line.explanation or "").strip()),
        }
        party["items"].append(item)
        if bucket == "180+":
            explanation_state = "aged but explained" if item["explanation_on_file"] else "aged, no explanation on file"
            oldest_reviews.append({
                **item,
                "party_name": line.party_name,
                "review_note": f"Balance is more than 180 days past the selected ageing date; {explanation_state}. Human review required.",
            })

    return {
        "as_of_date": as_of_date.isoformat(),
        "account_type": account_type,
        "ageing_date_policy": "Use due_date when supplied; otherwise use invoice_date. Future dates are shown in 0-30 with not_yet_due=true.",
        "bucket_totals": {bucket: str(value) for bucket, value in totals.items()},
        "total_open_balance": str(sum(totals.values(), Decimal("0.00"))),
        "counterparties": [
            {
                "party_name": name,
                "bucket_totals": {bucket: str(value) for bucket, value in entry["buckets"].items()},
                "items": entry["items"],
            }
            for name, entry in sorted(parties.items(), key=lambda item: item[0].casefold())
        ],
        "oldest_bucket_reviews": oldest_reviews,
        "professional_review_required": True,
        "source_limitation": "This schedule reflects only the uploaded open-item ledger rows and does not establish completeness or reconcile to the trial balance.",
    }
