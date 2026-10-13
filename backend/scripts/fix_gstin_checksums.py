"""
Repairs the check digit on invented GSTINs in a purchase register.

Why this exists: a GSTIN's fifteenth character is a modulus-36 checksum over
the first fourteen, so hand-written test identifiers such as
'27AABCO5678D1Z8' are almost always rejected. Recomputing the check digit
keeps the identifier fictional but structurally valid, which is what a test
file needs.

This only fixes the check digit. A GSTIN whose shape is wrong, or whose
state code does not exist, is reported and left alone, because those are not
recoverable by recomputation.

Usage:
    python scripts/fix_gstin_checksums.py <file.csv> [--in-place]
    python scripts/fix_gstin_checksums.py <file.xlsx> [--column vendor_gstin]

Without --in-place a corrected copy is written next to the original, named
`<name>.fixed.csv`, and the original is left untouched.
"""
import argparse
import os
import re
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import pandas as pd  # noqa: E402

from app.validation import (  # noqa: E402
    GSTIN_RE, gstin_checksum, normalize_gstin, validate_gstin,
)


def repair(raw: str) -> tuple[str, str]:
    """Return (value, outcome) where outcome is ok | fixed | unfixable."""
    gstin = normalize_gstin(raw)
    if not gstin:
        return raw, "unfixable"
    if validate_gstin(gstin)[0]:
        return gstin, "ok"
    # Only a well-formed GSTIN with a wrong final character can be repaired.
    if len(gstin) == 15 and GSTIN_RE.match(gstin[:14] + "0"):
        return gstin[:14] + gstin_checksum(gstin[:14]), "fixed"
    if len(gstin) == 14 and GSTIN_RE.match(gstin + "0"):
        return gstin + gstin_checksum(gstin), "fixed"
    return gstin, "unfixable"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="CSV or XLSX purchase register")
    ap.add_argument("--column", default="vendor_gstin",
                    help="GSTIN column name (default: vendor_gstin)")
    ap.add_argument("--in-place", action="store_true",
                    help="overwrite the original instead of writing a copy")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        raise SystemExit(f"No such file: {args.path}")

    lower = args.path.lower()
    if lower.endswith(".csv"):
        df = pd.read_csv(args.path, dtype=str, keep_default_na=False)
    elif lower.endswith((".xlsx", ".xls")):
        df = pd.read_excel(args.path, dtype=str)
    else:
        raise SystemExit("Expected a .csv or .xlsx file.")

    # Column matching is forgiving about case and spacing.
    wanted = re.sub(r"[\s_]+", "", args.column.strip().lower())
    match = next(
        (c for c in df.columns
         if re.sub(r"[\s_]+", "", str(c).strip().lower()) == wanted), None,
    )
    if match is None:
        raise SystemExit(
            f"No column named '{args.column}'. Columns present: {', '.join(map(str, df.columns))}"
        )

    counts = {"ok": 0, "fixed": 0, "unfixable": 0}
    changes: list[tuple[int, str, str]] = []
    unfixable: list[tuple[int, str]] = []
    new_values = []

    for idx, raw in enumerate(df[match].tolist()):
        value, outcome = repair(str(raw))
        counts[outcome] += 1
        excel_row = idx + 2   # +1 for zero-index, +1 for the header
        if outcome == "fixed":
            changes.append((excel_row, normalize_gstin(str(raw)), value))
        elif outcome == "unfixable":
            unfixable.append((excel_row, str(raw)))
        new_values.append(value)

    df[match] = new_values

    if args.in_place:
        out = args.path
    else:
        stem, ext = os.path.splitext(args.path)
        out = f"{stem}.fixed{ext if ext.lower() == '.csv' else '.csv'}"

    df.to_csv(out, index=False) if out.lower().endswith(".csv") else df.to_excel(out, index=False)

    print(f"Read {len(df)} rows from {args.path}")
    print(f"  already valid : {counts['ok']}")
    print(f"  repaired      : {counts['fixed']}")
    print(f"  unrepairable  : {counts['unfixable']}")

    if changes:
        print("\nRepaired check digits:")
        for row, before, after in changes[:20]:
            print(f"  row {row:<4} {before} -> {after}")
        if len(changes) > 20:
            print(f"  ... and {len(changes) - 20} more")

    if unfixable:
        print("\nLeft alone (wrong length, shape or state code, so not recoverable):")
        for row, value in unfixable[:20]:
            print(f"  row {row:<4} {value!r}")
        if len(unfixable) > 20:
            print(f"  ... and {len(unfixable) - 20} more")

    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
