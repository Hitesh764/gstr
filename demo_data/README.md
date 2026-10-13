# Demo data

Ready-to-upload files for testing the reconciliation flow.

| File | What it is |
|---|---|
| `purchase_register_2026-03.csv` | 36 purchase invoices, all valid |
| `gstr2b_2026-03.json` | The matching GSTR-2B statement |
| `purchase_register_with_errors.csv` | Deliberately broken, to see validation reporting |

## Important

`gstr2b_2026-03.json` is issued for GSTIN **27AAAAA0000A1Z2**, which is the
seeded demo client. Uploading it against a different GSTIN is rejected on
purpose: a GSTR-2B belonging to another registration would reconcile against
nothing and look like a mass filing failure, so the mismatch is caught at
upload instead.

If you want to use these files with your own client, either add a client
with GSTIN `27AAAAA0000A1Z2`, or edit the `gstin` field at the top of the JSON
to match yours.

## Using them

1. Sign in as `demo@sharma-ca.in`.
2. Open the client, choose a GSTIN, and go to Upload data.
3. Set the period to `2026-03`.
4. Drop `purchase_register_2026-03.csv` into the first box.
5. Drop `gstr2b_2026-03.json` into the second, or press Load sample GSTR-2B.
6. Run reconciliation.

Expect roughly 25 exact matches, 5 formatting-only differences, 4 invoices
the supplier has not filed, 2 value mismatches, and 2 invoices present in
GSTR-2B but missing from the books.

## Seeing validation work

Upload `purchase_register_with_errors.csv` instead. Nothing is imported and
each problem is reported against its spreadsheet row number: invalid GSTIN
checksum, empty vendor name, CGST/SGST combined with IGST, a currency
symbol, thousands separators, a future date, and a duplicate invoice. Two
rows import with warnings rather than errors, because a total that does not
add up and an invoice dated outside the period are both worth flagging but
neither is necessarily wrong.

## If your own test file has invalid GSTINs

Invented GSTINs almost never satisfy the modulus-36 check digit. To repair a
file rather than hand-editing it:

```bash
cd backend
python scripts/fix_gstin_checksums.py path/to/your.csv
```

That writes a corrected copy alongside the original and reports every change.
