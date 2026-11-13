"""Free try: one small PDF or CSV, no account. Shows the table, what is wrong with it, and the first rows.

Nothing is stored. The limits (size, pages, tries per hour, rows shown) live here so they can be changed in one place.
"""
from __future__ import annotations

import csv
import io
import re
import time

import pdf_tables

MAX_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_ROWS_COUNTED = 20000
PREVIEW_ROWS = 20
TRIES_PER_HOUR = 5
GLOBAL_TRIES_PER_HOUR = 400   # a ceiling for everyone together, in case the address header is faked

_tries: dict[str, list[float]] = {}
_all: list[float] = []


def allow_try(ip: str, now: float | None = None) -> tuple[bool, int]:
    """Returns (allowed, tries_left). Counts only allowed tries, per address, over the last hour."""
    now = time.time() if now is None else now
    if len(_tries) > 5000:
        for k in [k for k, v in _tries.items() if not v or now - v[-1] > 3600]:
            _tries.pop(k, None)
    _all[:] = [t for t in _all if now - t < 3600]
    if len(_all) >= GLOBAL_TRIES_PER_HOUR:
        return False, 0
    recent = [t for t in _tries.get(ip, []) if now - t < 3600]
    if len(recent) >= TRIES_PER_HOUR:
        _tries[ip] = recent
        return False, 0
    recent.append(now)
    _all.append(now)
    _tries[ip] = recent
    return True, TRIES_PER_HOUR - len(recent)


def _csv_rows(data: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text), dialect)):
        rows.append([c for c in r])
        if i >= MAX_ROWS_COUNTED:
            break
    return [r for r in rows if r]


_NUMTEXT = re.compile(r"^[\(\-]?[₹$€£]?\s?\d{1,3}(,\d{2,3})+(\.\d+)?\)?$|^\(\d+(\.\d+)?\)$|^[₹$€£]\s?\d+(\.\d+)?$")
_DATE_SHAPES = [
    ("yyyy-mm-dd", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")),
    ("dd/mm/yyyy", re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")),
    ("dd-mm-yyyy", re.compile(r"^\d{1,2}-\d{1,2}-\d{2,4}$")),
    ("dd.mm.yyyy", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")),
    ("d Mon yyyy", re.compile(r"^\d{1,2}[ -][A-Za-z]{3,9}[ -,]*\d{2,4}$")),
]


def problems(rows: list[list[str]]) -> dict:
    """Counts of things that are plainly wrong. The first row is treated as the header."""
    if len(rows) < 2:
        return {"total": 0, "items": []}
    head, body = rows[0], rows[1:]
    width = len(head)
    items = []

    empty_rows = sum(1 for r in body if all(not c.strip() for c in r))
    blanks = sum(1 for r in body if not all(not c.strip() for c in r) for c in (r + [""] * (width - len(r)))[:width] if not c.strip())
    seen, dups = set(), 0
    for r in body:
        k = tuple(c.strip().lower() for c in r)
        if all(not c for c in k):
            continue
        if k in seen:
            dups += 1
        seen.add(k)
    spaces = sum(1 for r in body for c in r if c != c.strip() or "  " in c)
    numtext = sum(1 for r in body for c in r if _NUMTEXT.match(c.strip()))

    mixed = 0
    for j in range(width):
        col = [r[j].strip() for r in body if j < len(r) and r[j].strip()]
        if len(col) < 3:
            continue
        shapes = [next((n for n, rx in _DATE_SHAPES if rx.match(c)), None) for c in col]
        dated = [s for s in shapes if s]
        if len(dated) / len(col) >= 0.6:
            counts = {}
            for s in dated:
                counts[s] = counts.get(s, 0) + 1
            if len(counts) > 1:
                mixed += len(dated) - max(counts.values())

    for label, n in (("Blank cells", blanks), ("Duplicate rows", dups), ("Empty rows", empty_rows),
                     ("Extra spaces", spaces), ("Numbers stored as text", numtext), ("Dates in mixed formats", mixed)):
        if n:
            items.append({"label": label, "count": n})
    return {"total": sum(i["count"] for i in items), "items": items}


def preview(filename: str, data: bytes) -> dict:
    name = (filename or "").lower()
    if len(data) > MAX_BYTES:
        return {"ok": False, "code": "too_big", "error": "The free try takes files up to 5 MB. Create a free account to use larger files."}
    if data.startswith(b"%PDF"):
        res = pdf_tables.extract_tables(data, max_pages=MAX_PDF_PAGES)
        if not res.get("ok"):
            return res
        t = res["tables"][0]
        rows, kind = t["rows"], "pdf"
        extra = {"pages": res["pages"], "pagesRead": min(res["pages"], MAX_PDF_PAGES), "otherTables": len(res["tables"]) - 1}
    elif name.endswith((".csv", ".tsv", ".txt")) or b"," in data[:2000] or b";" in data[:2000] or b"\t" in data[:2000]:
        rows, kind, extra = _csv_rows(data), "csv", {}
        if len(rows) < 2:
            return {"ok": False, "code": "no_table", "error": "No table was found in this file. A CSV needs a header row and at least one row of data."}
    else:
        return {"ok": False, "code": "type", "error": "The free try reads PDF and CSV files."}
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return {
        "ok": True, "kind": kind, "totalRows": len(rows) - 1, "cols": width,
        "rows": rows[: PREVIEW_ROWS + 1], "shown": min(PREVIEW_ROWS, len(rows) - 1),
        "problems": problems(rows), **extra,
    }
