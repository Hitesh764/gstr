"""
Money handling helpers.

Financial columns are stored as NUMERIC(14, 2) so that rupee amounts are
exact and do not accumulate binary floating-point drift across sums.
SQLAlchemy returns those columns as `decimal.Decimal`.

Decimal and float cannot be mixed in arithmetic (`Decimal - float` raises
TypeError), so these helpers define the two conversion points:

  * `to_decimal` — at the boundary where values enter the database.
  * `to_float`   — inside the matching engine, whose tolerance arithmetic
                   is comparison-only and where sub-paisa precision is
                   irrelevant (tolerances are whole rupees).
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

Number = Union[int, float, str, Decimal, None]

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def to_decimal(value: Number, default: Decimal = ZERO) -> Decimal:
    """Coerce any numeric-ish value to a 2-decimal-place Decimal."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        # via str so 0.1 becomes Decimal('0.1'), not the binary expansion
        d = Decimal(str(value))
    else:
        try:
            d = Decimal(str(value))
        except Exception:
            return default
    return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_float(value: Number, default: float = 0.0) -> float:
    """Coerce to plain float, for tolerance comparisons and JSON output."""
    if value is None:
        return default
    if isinstance(value, float):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def money_str(value: Number) -> str:
    """Format an amount using the Indian digit grouping system.

    Indian convention groups the last three digits, then every two
    thereafter: 10,00,000 rather than 1,000,000. Python's `{:,}` only
    produces the Western grouping, so the split is done by hand. A CA
    reading "possible unrecorded credit note of Rs 1,02,022.90" should not
    have to mentally re-group the digits.
    """
    amount = to_float(value)
    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):.2f}".partition(".")

    if len(whole) <= 3:
        grouped = whole
    else:
        last3 = whole[-3:]
        rest = whole[:-3]
        # Group the remaining digits in pairs, from the right.
        pairs = []
        while len(rest) > 2:
            pairs.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            pairs.insert(0, rest)
        grouped = ",".join(pairs) + "," + last3

    return f"{sign}{grouped}.{frac}"


def safe_sum(values) -> Decimal:
    """Sum an iterable of numeric-ish values exactly, as Decimal."""
    total = ZERO
    for v in values:
        total += to_decimal(v)
    return total
