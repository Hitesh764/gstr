"""Pure, deterministic ratio calculations over explicitly mapped TB accounts."""
from decimal import Decimal, ROUND_HALF_UP

RATIO_NAMES = (
    "current_ratio", "quick_ratio", "debt_equity_ratio", "inventory_turnover",
    "debtor_turnover", "gross_profit_margin", "net_profit_margin",
)


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if value is not None else None


def _div(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _q(numerator / denominator)


def _amount(lines_by_code: dict, codes: list[str], *, period: str) -> Decimal:
    total = Decimal("0")
    for code in codes:
        line = lines_by_code.get(code.casefold())
        if line is None:
            continue
        value = getattr(line, period)
        if value is not None:
            total += abs(Decimal(value))
    return total


def calculate_audit_ratios(*, closing_lines: list, opening_lines: list,
                           account_mapping: dict[str, list[str]],
                           prior_opening_inventory: Decimal | None = None,
                           prior_opening_receivables: Decimal | None = None,
                           movement_threshold_pct: Decimal) -> dict:
    closing_by_code = {line.account_code.casefold(): line for line in closing_lines}
    opening_by_code = {line.account_code.casefold(): line for line in opening_lines}
    current = {key: _amount(closing_by_code, codes, period="current_year_amount")
               for key, codes in account_mapping.items()}
    prior = {key: _amount(closing_by_code, codes, period="prior_year_amount")
             for key, codes in account_mapping.items()}

    def year_values(values, *, opening_inventory, opening_receivables, period):
        revenue = values["revenue"]
        current_assets = values["current_assets"]
        inventory = values["inventory"]
        liabilities = values["current_liabilities"]
        borrowings = values["borrowings"]
        equity = values["equity"]
        cogs = values["cost_of_goods_sold"]
        credit_sales = values["credit_sales"]
        receivables = values["trade_receivables"]
        # TB ingestion uses debit-positive / credit-negative signed balances.
        net_profit = -sum(
            Decimal(getattr(closing_by_code.get(code.casefold()), period) or 0)
            for code in account_mapping["net_profit"]
            if closing_by_code.get(code.casefold()) is not None
        )
        avg_inventory = ((opening_inventory + inventory) / 2) if opening_inventory is not None else None
        avg_receivables = ((opening_receivables + receivables) / 2) if opening_receivables is not None else None
        return {
            "current_ratio": _div(current_assets, liabilities),
            "quick_ratio": _div(current_assets - inventory, liabilities),
            "debt_equity_ratio": _div(borrowings, equity),
            "inventory_turnover": _div(cogs, avg_inventory),
            "debtor_turnover": _div(credit_sales, avg_receivables),
            "gross_profit_margin": _div(revenue - cogs, revenue),
            "net_profit_margin": _div(net_profit, revenue),
        }

    def opening_total(codes):
        # Missing mapped rows are unknown, not zero balances.
        if not opening_lines or any(code.casefold() not in opening_by_code for code in codes):
            return None
        return _amount(opening_by_code, codes, period="current_year_amount")

    current_open_inventory = opening_total(account_mapping["inventory"])
    current_open_receivables = opening_total(account_mapping["trade_receivables"])
    current_ratios = year_values(current, opening_inventory=current_open_inventory,
                                 opening_receivables=current_open_receivables,
                                 period="current_year_amount")
    prior_ratios = year_values(prior, opening_inventory=prior_opening_inventory,
                               opening_receivables=prior_opening_receivables,
                               period="prior_year_amount")

    results = {}
    for name in RATIO_NAMES:
        now, before = current_ratios[name], prior_ratios[name]
        movement = None
        if now is not None and before not in (None, Decimal("0.0000")):
            movement = _q((now - before) / abs(before) * Decimal("100"))
        threshold_exceeded = movement is not None and abs(movement) > Decimal(movement_threshold_pct)
        missing_reason = None
        if now is None or before is None:
            if name in {"inventory_turnover", "debtor_turnover"}:
                missing_reason = "Opening balance is missing for one or both years; turnover was not estimated from closing balances."
            elif name in {"current_ratio", "quick_ratio", "debt_equity_ratio"} and ((now is None and current_ratios.get(name) is None) or before is None):
                missing_reason = "The mapped denominator is zero or the required comparative balance is unavailable."
            else:
                missing_reason = "The mapped denominator is zero or required source data is unavailable."
        results[name] = {
            "current_year": str(now) if now is not None else None,
            "prior_year": str(before) if before is not None else None,
            "movement_pct": str(movement) if movement is not None else None,
            "movement_threshold_pct": str(movement_threshold_pct),
            "threshold_exceeded": threshold_exceeded,
            "risk_flag_id": None,
            "unavailable_reason": missing_reason,
        }
    return {
        "ratios": results,
        "accounting_sign_convention": "Trial-balance mappings assume debit-positive and credit-negative source amounts; credit-normal net-profit balances are sign-reversed for presentation.",
        "turnover_opening_balance_source": "Current-year opening schedule uses the opening trial-balance upload. Prior-year opening inventory and receivables must be explicitly supplied with a source reference; otherwise prior-year turnover is unavailable.",
        "judgment_boundary": "Ratios and movements are mechanical indicators. They do not establish normality, cause, going concern, fraud, or an audit conclusion.",
    }
