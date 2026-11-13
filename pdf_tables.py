"""PDF -> plain tables (any text-based PDF). No AI: the PDF's own text is read with pdfplumber and nothing is stored.

Two ways of finding a table are tried on every page: ruled lines first (cells drawn with borders), then
text alignment (columns that only line up by spacing). Tables that carry the same header across pages
are joined so a long report comes back as one table, not twenty fragments.
"""
from __future__ import annotations

import io
import re

import pdfplumber

MAX_PAGES = 60
MAX_TABLES = 12
MAX_ROWS = 20000
MAX_COLS = 60

_LINES = {"vertical_strategy": "lines", "horizontal_strategy": "lines", "snap_tolerance": 3, "join_tolerance": 3}


def _clean_cell(v) -> str:
    if v is None:
        return ""
    return re.sub(r"[ \t]+", " ", str(v).replace("\r", "").replace("\n", " ")).strip()


def _tidy(raw: list[list]) -> list[list[str]]:
    rows = [[_clean_cell(c) for c in r] for r in raw if r]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j] for r in rows)]   # drop columns that are empty everywhere
    return [[r[j] for j in keep] for r in rows]


def _good(rows: list[list[str]]) -> bool:
    if len(rows) < 2 or not rows[0] or len(rows[0]) < 2:
        return False
    filled = sum(1 for r in rows for c in r if c)
    return filled / (len(rows) * len(rows[0])) >= 0.35       # mostly empty grids are layout, not data


def _text_table(page) -> list[list[str]]:
    """Borderless table: columns are the empty vertical strips that run (almost) the whole height of the text."""
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    if len(words) < 8:
        return []
    words.sort(key=lambda w: (round(w["top"] / 3), w["x0"]))
    lines: list[list[dict]] = []
    last_key = None
    for w in words:
        k = round(w["top"] / 3)
        if last_key is None or abs(k - last_key) > 1:
            lines.append([])
        lines[-1].append(w)
        last_key = k
    if len(lines) < 3:
        return []
    width = int(page.width) + 1
    cover = [0] * width
    for ln in lines:
        seen = set()
        for w in ln:
            for x in range(max(0, int(w["x0"])), min(width, int(w["x1"]) + 1)):
                seen.add(x)
        for x in seen:
            cover[x] += 1
    xs = [x for x in range(width) if cover[x]]
    if not xs:
        return []
    lo, hi = xs[0], xs[-1]
    limit = max(1, int(0.08 * len(lines)))          # a title or note line may cross a gap
    cuts, run = [], []
    for x in range(lo, hi + 1):
        if cover[x] <= limit:
            run.append(x)
        else:
            if len(run) >= 8:
                cuts.append((run[0] + run[-1]) / 2)
            run = []
    if len(cuts) < 1:
        return []
    def col_of(w):
        mid = (w["x0"] + w["x1"]) / 2
        return sum(1 for c in cuts if mid > c)
    rows = []
    for ln in lines:
        cells = [[] for _ in range(len(cuts) + 1)]
        for w in ln:
            cells[col_of(w)].append(w["text"])
        rows.append([" ".join(c) for c in cells])
    rows = _tidy(rows)
    if not rows:
        return []
    ncol = len(rows[0])
    need = max(2, int(0.6 * ncol + 0.5))
    fill = lambda r: sum(1 for c in r if c)
    while rows and fill(rows[0]) < need:
        rows.pop(0)
    while rows and fill(rows[-1]) < need:
        rows.pop()
    return rows


def _page_tables(page) -> list[list[list[str]]]:
    try:
        tabs = page.extract_tables(_LINES)
    except Exception:
        tabs = []
    found = [t for t in (_tidy(x) for x in tabs) if _good(t)]
    if found:
        return found                                   # ruled tables are the more reliable reading
    try:
        rows = _text_table(page)
    except Exception:
        rows = []
    return [rows] if _good(rows) else []


def extract_tables(data: bytes, max_pages: int = MAX_PAGES) -> dict:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:
        return {"ok": False, "code": "unreadable", "error": f"This PDF could not be opened ({type(e).__name__}). "
                                                              "If it is password-protected, remove the password first."}
    with pdf:
        n_pages = len(pdf.pages)
        if n_pages == 0:
            return {"ok": False, "code": "unreadable", "error": "This PDF has no pages."}
        chars = sum(len(p.chars) for p in pdf.pages[:5])
        if chars < 40:
            return {"ok": False, "code": "scanned",
                    "error": "This looks like a scanned or photographed PDF: it has no text to read. Scanned files "
                             "are not supported yet. Use the original PDF (exported from the program that made it)."}
        tables: list[dict] = []      # {pages:set, rows, head}
        for pno, page in enumerate(pdf.pages[:max_pages], start=1):
            for rows in _page_tables(page):
                head = tuple(c.lower() for c in rows[0])
                last = tables[-1] if tables else None
                if last and len(last["rows"][0]) == len(rows[0]) and last["pages"][-1] == pno - 1 and \
                        (head == last["head"] or not any(re.search(r"[A-Za-z]", c) for c in rows[0]) or
                         sum(1 for c in rows[0] if re.fullmatch(r"[\d.,\-\s]*", c)) >= len(rows[0]) // 2):
                    body = rows[1:] if head == last["head"] else rows            # same header again: skip it
                    last["rows"].extend(body)
                    last["pages"].append(pno)
                else:
                    tables.append({"pages": [pno], "rows": rows, "head": head})
        truncated_pages = n_pages > max_pages
    if not tables:
        return {"ok": False, "code": "no_table",
                "error": "No table was found in this PDF. It may be plain text rather than a table, or a layout this "
                         "reader cannot follow. Try a PDF where the data sits in rows and columns."}
    out = []
    for i, t in enumerate(tables[:MAX_TABLES], start=1):
        rows = t["rows"][:MAX_ROWS]
        out.append({"id": i, "pages": [t["pages"][0], t["pages"][-1]], "rows": [r[:MAX_COLS] for r in rows],
                    "nRows": len(rows), "nCols": min(len(rows[0]), MAX_COLS),
                    "cut": len(t["rows"]) > MAX_ROWS})
    return {"ok": True, "tables": out, "pages": n_pages, "truncatedPages": truncated_pages,
            "moreTables": max(0, len(tables) - MAX_TABLES)}
