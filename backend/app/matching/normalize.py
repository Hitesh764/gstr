"""
Normalization helpers used by the matching engine.
"""
import re
from difflib import SequenceMatcher


def normalize_invoice_number(raw: str) -> str:
    """Strip spaces, slashes, hyphens, dots and case for comparison.

    'INV-2026-042' / 'INV/2026/0042' / 'inv 2026 042' all normalize to
    'INV2026042'.
    """
    if raw is None:
        return ""
    return re.sub(r"[\s/\-.]", "", str(raw)).upper()


def invoice_number_similarity(a: str, b: str) -> float:
    """0..1 similarity ratio between two normalized invoice numbers."""
    na, nb = normalize_invoice_number(a), normalize_invoice_number(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def normalize_gstin(raw: str) -> str:
    if raw is None:
        return ""
    return str(raw).strip().upper()
