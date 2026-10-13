"""
Validation for Indian tax identifiers and purchase-register rows.

GSTIN validation here is real, not a length check. A GSTIN is 15 characters:

    27  AAACS1234A  1        Z   5
    │   │           │        │   └── checksum
    │   │           │        └────── literal 'Z' (14th character)
    │   │           └─────────────── entity code for this PAN in this state
    │   └─────────────────────────── the holder's 10-character PAN
    └─────────────────────────────── state code, 01-38 (+97 for other territory)

The final character is a modulus-36 checksum over the first 14. Verifying it
catches transposed and mistyped digits that a regex cannot, which matters
because a wrong GSTIN silently produces a reconciliation that matches
nothing and looks like a supplier filing failure.
"""
import re
from datetime import date
from decimal import Decimal
from typing import Optional, Tuple

from app.money import money_str

# 0-9 then A-Z: the alphabet used for the checksum, value = index.
_CHECKSUM_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Valid GST state codes: 01-38, plus 97 (Other Territory) and 99 (Centre).
_VALID_STATE_CODES = {f"{i:02d}" for i in range(1, 39)} | {"97", "99"}

STATE_NAMES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "28": "Andhra Pradesh (Old)", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}


def normalize_gstin(raw: Optional[str]) -> str:
    """Upper-case and strip whitespace, including internal spaces."""
    if raw is None:
        return ""
    return re.sub(r"\s+", "", str(raw)).upper()


def gstin_checksum(first_14: str) -> str:
    """Compute the 15th character of a GSTIN.

    Each of the first 14 characters is converted to its value in the
    36-character alphabet, multiplied by an alternating weight of 1 and 2,
    and the product's quotient and remainder over 36 are summed. The check
    digit is whatever brings that total up to the next multiple of 36.
    """
    total = 0
    for i, ch in enumerate(first_14):
        value = _CHECKSUM_ALPHABET.index(ch)
        weight = 2 if i % 2 else 1
        product = value * weight
        total += product // 36 + product % 36
    checksum_value = (36 - (total % 36)) % 36
    return _CHECKSUM_ALPHABET[checksum_value]


def validate_gstin(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Returns (ok, error_message). The message is shown to the user, so it
    explains which part is wrong rather than saying "invalid GSTIN"."""
    gstin = normalize_gstin(raw)

    if not gstin:
        return False, "GSTIN is required."
    if len(gstin) != 15:
        return False, f"GSTIN must be exactly 15 characters — got {len(gstin)} ('{gstin}')."
    if not GSTIN_RE.match(gstin):
        return False, (
            f"'{gstin}' is not a valid GSTIN format. Expected 2 digits (state), "
            f"10-character PAN, 1 entity character, 'Z', then 1 check character."
        )

    state = gstin[:2]
    if state not in _VALID_STATE_CODES:
        return False, f"'{state}' is not a valid GST state code (expected 01-38, 97 or 99)."

    expected = gstin_checksum(gstin[:14])
    if gstin[14] != expected:
        return False, (
            f"GSTIN '{gstin}' has an invalid check digit — it ends in '{gstin[14]}' "
            f"but should end in '{expected}'. This is usually a typo or a transposed character."
        )
    return True, None


def is_valid_gstin(raw: Optional[str]) -> bool:
    return validate_gstin(raw)[0]


def state_from_gstin(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    gstin = normalize_gstin(raw)
    if len(gstin) < 2:
        return None, None
    code = gstin[:2]
    return code, STATE_NAMES.get(code)


def pan_from_gstin(raw: Optional[str]) -> Optional[str]:
    gstin = normalize_gstin(raw)
    return gstin[2:12] if len(gstin) >= 12 else None


def validate_pan(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    if not raw:
        return False, "PAN is required."
    pan = str(raw).strip().upper()
    if len(pan) != 10:
        return False, f"PAN must be exactly 10 characters — got {len(pan)}."
    if not PAN_RE.match(pan):
        return False, (
            f"'{pan}' is not a valid PAN format. Expected 5 letters, 4 digits, then 1 letter."
        )
    return True, None


# ---------------------------------------------------------------------------
# Purchase register row validation
# ---------------------------------------------------------------------------

def period_bounds(period: str) -> Tuple[Optional[date], Optional[date]]:
    """First and last day of a 'YYYY-MM' period."""
    try:
        year, month = period.split("-")
        year, month = int(year), int(month)
        start = date(year, month, 1)
        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, month + 1, 1).replace(day=1)
            end = date(end.year, end.month, 1)
            from datetime import timedelta
            end = end - timedelta(days=1)
        return start, end
    except (ValueError, AttributeError):
        return None, None


def validate_tax_split(
    cgst: Decimal, sgst: Decimal, igst: Decimal,
) -> Tuple[bool, Optional[str]]:
    """A supply is either intra-state (CGST+SGST) or inter-state (IGST).

    Both populated on one invoice is contradictory and usually means the
    row was mapped from the wrong columns, which would quietly overstate
    the ITC claimed.
    """
    has_intra = (cgst or 0) > 0 or (sgst or 0) > 0
    has_inter = (igst or 0) > 0
    if has_intra and has_inter:
        return False, (
            f"Row has both CGST/SGST (₹{money_str(cgst or 0)}/₹{money_str(sgst or 0)}) "
            f"and IGST (₹{money_str(igst or 0)}). A supply is either intra-state or "
            f"inter-state, never both — check the column mapping."
        )
    return True, None


def validate_invoice_date(
    invoice_date: date, period: str,
) -> Tuple[bool, Optional[str], str]:
    """Returns (ok, message, severity).

    A date outside the period is a warning, not an error: an invoice dated
    in a prior month legitimately appears in a later return when it was
    received late. A future date is an error, because it cannot be genuine.
    """
    if invoice_date > date.today():
        return False, (
            f"Invoice date {invoice_date.isoformat()} is in the future."
        ), "error"

    start, end = period_bounds(period)
    if start and end and not (start <= invoice_date <= end):
        return True, (
            f"Invoice date {invoice_date.isoformat()} falls outside period {period} "
            f"({start.isoformat()} to {end.isoformat()}). This is allowed — a late-received "
            f"invoice may be claimed in a later period — but confirm it is intended."
        ), "warning"

    if invoice_date.year < 2017:
        return False, (
            f"Invoice date {invoice_date.isoformat()} predates GST (July 2017)."
        ), "error"

    return True, None, "ok"


def looks_like_number(raw) -> Tuple[bool, Optional[str]]:
    """Reject currency symbols and thousands separators rather than coercing.

    Silently stripping '₹' and ',' hides a genuinely malformed export. A CA
    needs to know the source file is wrong, not have it quietly fixed.
    """
    if raw is None:
        return True, None
    s = str(raw).strip()
    if s == "" or s.lower() == "nan":
        return True, None
    if re.search(r"[₹$€£]", s):
        return False, f"'{s}' contains a currency symbol. Provide a plain number."
    if "," in s:
        return False, f"'{s}' contains thousands separators. Provide a plain number."
    if re.match(r"^\(.*\)$", s):
        return False, f"'{s}' uses parentheses for a negative. Use a minus sign."
    try:
        float(s)
        return True, None
    except ValueError:
        return False, f"'{s}' is not a number."
