"""Run the bank statement reader over a folder of PDFs and report how each one did.

  python check_statements.py path/to/folder
  python check_statements.py path/to/folder --password secret      (same password for every file)
  python check_statements.py path/to/folder --flagged              (also list the flagged rows)

For each PDF it prints the label (verified, partly_verified, unverified, needs_review or a failure code), the row counts, and the
file-level notes. It also writes statement_check_results.csv next to where you run it, so you can keep a record.

The reader works from the PDF's own text and the statement's printed balances, with no AI. Nothing is uploaded or stored:
this runs on your own computer. Use real statements, but keep them private.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import pdf_statement


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--password", default=None)
    ap.add_argument("--flagged", action="store_true", help="list the flagged rows for each file")
    a = ap.parse_args()
    files = sorted(p for p in Path(a.folder).rglob("*") if p.suffix.lower() == ".pdf")
    if not files:
        print("No PDF files found in that folder.")
        return 1
    out, counts = [], {}
    for f in files:
        t0 = time.time()
        try:
            res = pdf_statement.extract_statement(f.read_bytes(), None, False, a.password)
        except Exception as e:  # a crash is a result too
            res = {"ok": False, "code": "crashed", "error": f"{type(e).__name__}: {e}"}
        secs = round(time.time() - t0, 1)
        if res.get("ok"):
            s = res["summary"]
            label, rows, ver, flg, unc = s["label"], s["rows"], s["verified"], s["flagged"], s["unchecked"]
            notes = "; ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in s.get("fileFlags", []))
            date_note = "" if res.get("dateOrderCertain") else "date order not certain"
            notes = "; ".join(x for x in (notes, date_note) if x)
        else:
            label, rows, ver, flg, unc, notes = res.get("code", "failed"), 0, 0, 0, 0, res.get("error", "")
        counts[label] = counts.get(label, 0) + 1
        out.append([str(f), label, rows, ver, flg, unc, secs, notes])
        print(f"{label:<16} rows={rows:<5} verified={ver:<5} flagged={flg:<4} unchecked={unc:<4} {secs}s  {f.name}")
        if notes:
            print(f"                 note: {notes}")
        if a.flagged and res.get("ok"):
            for r in res["rows"]:
                if r["status"] == "flagged":
                    why = "; ".join(x.get("message", "") if isinstance(x, dict) else str(x) for x in r["flags"])
                    print(f"                 row {r['id']} p{r['page']} {r['date']} {r['description'][:40]!r}: {why}")
    with open("statement_check_results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "label", "rows", "verified", "flagged", "unchecked", "seconds", "notes"])
        w.writerows(out)
    n = len(files)
    print("\nSummary over", n, "file(s):")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v} ({round(100 * v / n)}%)")
    print("\nA 'verified' label means the statement's own balances agree with every row. It does not mean the text of the"
          "\ndescriptions is right: still compare a sample of rows with the PDF by eye. Details saved to statement_check_results.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
