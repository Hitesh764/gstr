"""
Builds sample GST notices in `demo_data/` for trying the notice workflow.

    sample_notice_asmt10.pdf     a digital notice (has a text layer), for the demo
                                 client and the March 2026 data. The reply-by date
                                 is DAYS from today, so deadline alerts can be seen.
    sample_notice_scanned.pdf    a page with no text layer, which is what a scan
                                 looks like. Uploading it shows the "paste the text"
                                 path, since OCR is optional.

Run: python scripts/make_demo_notice.py [--days 5]

The notice is fictional. It names the demo client's GSTIN, taken from
demo_data/gstr2b_2026-03.json, so the reconciliation records for that client
are what a drafted response is built from.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from tests.notice_helpers import make_blank_pdf, make_pdf  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "demo_data")


def build(days: int) -> list[str]:
    with open(os.path.join(OUT_DIR, "gstr2b_2026-03.json"), encoding="utf-8") as fh:
        gstin = json.load(fh)["gstin"]
    today = date.today()
    reply_by = today + timedelta(days=days)
    return [
        "OFFICE OF THE ASSISTANT COMMISSIONER OF STATE TAX",
        "Government of Maharashtra, GST Department",
        "",
        "FORM GST ASMT-10",
        "Notice for intimating discrepancies in the return after scrutiny",
        "[Section 61 of the Maharashtra Goods and Services Tax Act, 2017]",
        "",
        "Notice No: ASMT10/MH/2026/004217",
        f"Date: {today.strftime('%d/%m/%Y')}",
        "",
        "To,",
        "Shree Ganesh Manufacturing Co.",
        f"GSTIN: {gstin}",
        "Tax period: March 2026",
        "",
        "On scrutiny of your returns for the tax period stated above, the following",
        "discrepancies have been noticed:",
        "",
        "1. Explain the difference between the input tax credit claimed in GSTR-3B and the",
        "   credit available in your GSTR-2B for the period.",
        "2. Furnish a reconciliation of purchases from suppliers whose invoices do not",
        "   appear in your GSTR-2B.",
        "3. Provide the reasons for the value differences on invoices that do appear.",
        "",
        "Amount of input tax credit in question: Rs. 1,80,000",
        "",
        f"You are requested to submit your reply on or before {reply_by.strftime('%d/%m/%Y')}.",
        "If you do not, proceedings may be initiated under Section 73 of the Act.",
        "",
        "Proper Officer",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--days", type=int, default=5, help="days from today until the reply-by date")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    lines = build(args.days)
    with open(os.path.join(OUT_DIR, "sample_notice_asmt10.pdf"), "wb") as fh:
        fh.write(make_pdf(lines))
    with open(os.path.join(OUT_DIR, "sample_notice_scanned.pdf"), "wb") as fh:
        fh.write(make_blank_pdf())
    print(f"Wrote demo_data/sample_notice_asmt10.pdf (reply due {args.days} days from today)")
    print("Wrote demo_data/sample_notice_scanned.pdf (no text layer)")


if __name__ == "__main__":
    main()
