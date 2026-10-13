"""Mechanical audit planning suggestions; professional decisions remain human."""
from decimal import Decimal, ROUND_HALF_UP


def calculate_materiality(basis_amount: Decimal, percentage: Decimal) -> Decimal:
    if basis_amount <= 0 or percentage <= 0 or percentage > 100:
        raise ValueError("A positive financial base and configured percentage are required.")
    return (basis_amount * percentage / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def assess_caro(rule_data: dict, inputs: dict) -> tuple[bool, list[int], str]:
    """Evaluate versioned rule data; the data, not the application, owns thresholds."""
    if inputs["is_consolidated_financial_statements"]:
        clauses = [int(n) for n in rule_data["consolidated_only_clause_numbers"]]
        return True, clauses, "For consolidated financial statements, the configured CARO rule limits reporting scope to the listed clause(s); partner confirmation is required."

    exclusions = rule_data.get("exempt_boolean_fields", [])
    active_exclusions = [item["label"] for item in exclusions if inputs.get(item["field"], False)]
    if active_exclusions:
        return False, [], "System applicability indication based on supplied classification: " + "; ".join(active_exclusions) + ". Partner must verify classification and confirm."

    private = rule_data["private_company_exemption"]
    criteria = private["criteria"]
    private_exempt = (
        inputs.get(private["private_field"], False)
        and not inputs.get(private["public_group_field"], False)
        and all(Decimal(str(inputs[c["field"]])) <= Decimal(str(c["value"]))
                for c in criteria if c["operator"] == "lte")
        and all(c["operator"] == "lte" for c in criteria)
    )
    if private_exempt:
        reasons = "; ".join(
            f"{c['label']} is within the configured limit {c['value']}" for c in criteria
        )
        return False, [], f"System applicability indication: private company criteria met ({reasons}). Partner must verify and confirm."
    clauses = [int(n) for n in rule_data["all_clause_numbers"]]
    return True, clauses, "System applicability indication: no configured exclusion was met; all configured clauses are in scope, subject to partner confirmation."


def variance_flag_candidate(line, *, percentage_threshold: Decimal,
                            materiality_amount: Decimal | None,
                            materiality_multiplier: Decimal) -> tuple[bool, dict]:
    """Apply only firm-configured mechanical triggers to one account movement."""
    variance = Decimal(line.variance_amount or 0)
    pct = Decimal(line.variance_pct) if line.variance_pct is not None else None
    absolute_threshold = (
        Decimal(materiality_amount) * Decimal(materiality_multiplier)
        if materiality_amount is not None else None
    )
    pct_triggered = pct is not None and abs(pct) > Decimal(percentage_threshold)
    amount_triggered = absolute_threshold is not None and abs(variance) > absolute_threshold
    return bool(pct_triggered or amount_triggered), {
        "current_year_amount": str(line.current_year_amount or 0),
        "prior_year_amount": str(line.prior_year_amount or 0),
        "variance_amount": str(variance),
        "variance_pct": str(pct) if pct is not None else None,
        "percentage_threshold": str(percentage_threshold),
        "materiality_amount": str(materiality_amount) if materiality_amount is not None else None,
        "materiality_multiplier": str(materiality_multiplier),
        "absolute_threshold": str(absolute_threshold) if absolute_threshold is not None else None,
        "percentage_triggered": pct_triggered,
        "absolute_triggered": amount_triggered,
    }


def variance_system_note(line, trigger_data: dict, related_lines: list) -> str:
    """Describe source figures and related same-group movements without inferring cause."""
    amount = Decimal(trigger_data["variance_amount"])
    direction = "increased" if amount > 0 else "decreased" if amount < 0 else "changed"
    note = (
        f"SYSTEM FLAG — {line.account_name} ({line.account_code}) {direction} by "
        f"₹{abs(amount):,.2f} year-on-year, from ₹{Decimal(trigger_data['prior_year_amount']):,.2f} "
        f"to ₹{Decimal(trigger_data['current_year_amount']):,.2f}. "
    )
    pct = trigger_data["variance_pct"]
    note += f"Movement is {abs(Decimal(pct)):.2f}%" if pct is not None else "Prior-year balance was zero, so percentage movement is undefined"
    if trigger_data["percentage_triggered"] and trigger_data["absolute_triggered"]:
        note += "; it exceeded both configured movement triggers."
    elif trigger_data["percentage_triggered"]:
        note += "; it exceeded the configured percentage trigger."
    else:
        note += "; it exceeded the configured materiality-linked amount trigger."
    peers = [item.account_name for item in related_lines if item.id != line.id]
    if peers:
        note += " Other accounts in the same trial-balance group also moved: " + ", ".join(peers[:5]) + "."
    note += " This is a system-generated observation for CA review; no cause, misstatement, or audit conclusion is inferred."
    return note
