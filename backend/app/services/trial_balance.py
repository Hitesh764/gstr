"""Pure trial-balance comparison calculations."""
from decimal import Decimal

ZERO = Decimal("0.00")


def calculate_variance(current: Decimal, prior: Decimal) -> tuple[Decimal, Decimal | None]:
    """Return amount and percentage movement from prior to current.

    A percentage is undefined when the prior balance is zero and the current
    balance is non-zero; represent that as None rather than infinity or an
    invented percentage. A zero-to-zero movement is 0 percent.
    """
    current = Decimal(current).quantize(Decimal("0.01"))
    prior = Decimal(prior).quantize(Decimal("0.01"))
    amount = (current - prior).quantize(Decimal("0.01"))
    if prior == ZERO:
        return amount, Decimal("0.0000") if current == ZERO else None
    pct = (amount / prior.copy_abs() * Decimal("100")).quantize(Decimal("0.0001"))
    return amount, pct


def compare_trial_balances(current_rows: list[dict], prior_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Join two parsed snapshots by account code and calculate variances.

    Accounts present in only one period remain in the result with a zero
    balance for the other period. The returned notes are factual movement
    labels only; they do not infer audit risk or cause.
    """
    current = {row["account_code"].strip().casefold(): row for row in current_rows}
    prior = {row["account_code"].strip().casefold(): row for row in prior_rows}
    merged = []
    notes = []
    for key in sorted(set(current) | set(prior)):
        current_row = current.get(key)
        prior_row = prior.get(key)
        source = current_row or prior_row
        current_amount = current_row["amount"] if current_row else ZERO
        prior_amount = prior_row["amount"] if prior_row else ZERO
        variance_amount, variance_pct = calculate_variance(current_amount, prior_amount)
        merged.append({
            "account_code": source["account_code"],
            "account_name": source["account_name"],
            "account_group": (current_row or prior_row).get("account_group"),
            "current_year_amount": current_amount,
            "prior_year_amount": prior_amount,
            "variance_amount": variance_amount,
            "variance_pct": variance_pct,
            "source_row_number": (current_row or prior_row).get("source_row_number"),
        })
        if current_row is None:
            notes.append({"account_code": source["account_code"], "change": "ABSENT_CURRENT_YEAR"})
        elif prior_row is None:
            notes.append({"account_code": source["account_code"], "change": "NEW_CURRENT_YEAR"})
    return merged, notes
