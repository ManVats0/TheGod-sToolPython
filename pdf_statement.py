"""
Bank statement PDF -> table, with proof.

The promise is not "no errors". It is "no silent errors": every row is either shown to be consistent with
the statement's own printed balances, or it is flagged. Nothing is corrected quietly.

How it works (no AI is used, so the file never leaves this server):
  1. Reader A reads the PDF's own text layer with positions and rebuilds the table from the header row.
  2. Reader B reads the same pages again as plain text lines with a different method (no column logic).
     If the two disagree on a row's amount or balance, that row is flagged.
  3. The statement checks itself: previous balance + this row's amount must equal this row's balance.
     A break is pinned to the row, with a hint (sign swapped, digits swapped, row lost, duplicate).
  4. File-level checks: opening + all rows = closing, and printed totals if the statement has them.
  5. Dates: if day/month order is not certain from the data, the file is not marked verified.

A statement can only be proven if it prints balances. Without them every row is "unchecked" and the file
is labelled UNVERIFIED. Scanned files (no text layer) are refused, not guessed.
"""
from __future__ import annotations

import base64
import io
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import pdfplumber

MAX_PAGES = 60
CENT = Decimal("0.005")

# ── header vocabulary ─────────────────────────────────────────────────────────────────────────────────────────────

def _role_of(label: str) -> str | None:
    t = re.sub(r"[^a-z/ ]+", " ", label.lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    words = set(t.replace("/", " ").split())
    if re.search(r"\b(dr/cr|cr/dr)\b", t) or t in ("dr cr", "cr dr"):
        return "drcr"
    if "value" in words and ("date" in words or "dt" in words):
        return "value_date"
    if "balance" in words or "bal" in words:
        return "balance"
    if words & {"withdrawal", "withdrawals", "debit", "debits", "dr"} or "paid out" in t or "money out" in t:
        return "debit"
    if words & {"deposit", "deposits", "credit", "credits", "cr"} or "paid in" in t or "money in" in t:
        return "credit"
    if words & {"amount", "amt"}:
        return "amount"
    if words & {"date", "dt"}:
        return "date"
    if words & {"narration", "description", "particulars", "details", "remarks", "transaction"}:
        return "desc"
    if words & {"chq", "cheque", "ref", "reference", "utr", "instrument"}:
        return "ref"
    return None


# ── tokens: dates and amounts ─────────────────────────────────────────────────────────────────────────────────────

_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_RE_DATE_NUM = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$")
_RE_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_DATE_MON = re.compile(r"^(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-,]*(\d{2,4})$")
_RE_AMOUNT = re.compile(r"^\d+(\.\d+)?$|^\.\d+$")


def _year(y: int) -> int:
    if y < 100:
        return 2000 + y if y < 70 else 1900 + y
    return y


def looks_like_date(text: str) -> bool:
    t = text.strip()
    if _RE_DATE_NUM.match(t) or _RE_DATE_ISO.match(t):
        return True
    m = _RE_DATE_MON.match(t)
    return bool(m and m.group(2)[:3].lower() in _MONTHS)


def parse_date(text: str, order: str) -> date | None:
    t = text.strip()
    try:
        m = _RE_DATE_ISO.match(t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _RE_DATE_MON.match(t)
        if m and m.group(2)[:3].lower() in _MONTHS:
            return date(_year(int(m.group(3))), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        m = _RE_DATE_NUM.match(t)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), _year(int(m.group(3)))
            d, mo = (a, b) if order == "DMY" else (b, a)
            return date(y, mo, d)
    except ValueError:
        return None
    return None


def parse_amount(text: str) -> tuple[Decimal | None, str | None]:
    """(value, sign_hint). sign_hint is 'Dr' / 'Cr' when the text carries one. Value is always >= 0 here;
    a minus sign or brackets are reported as 'Dr'."""
    t = text.strip()
    if not t:
        return None, None
    hint = None
    m = re.search(r"\s*(dr|cr|d|c)\.?$", t, re.I)
    if m and re.search(r"\d", t[: m.start()]):
        hint = "Dr" if m.group(1).lower() in ("dr", "d") else "Cr"
        t = t[: m.start()]
    t = re.sub(r"(?i)\b(rs|inr|usd)\b\.?", "", t)
    t = t.replace("₹", "").replace("$", "").replace("£", "").replace("€", "").replace(" ", "")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.startswith("-"):
        neg, t = True, t[1:]
    if t.endswith("-"):
        neg, t = True, t[:-1]
    t = t.replace(",", "")
    if not _RE_AMOUNT.match(t):
        return None, None
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None, None
    if neg:
        hint = "Dr"
    return v, hint


def looks_like_amount(text: str) -> bool:
    v, _ = parse_amount(text)
    return v is not None and ("." in text or "," in text)


# ── layout helpers ────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class W:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


def _lines(words: list[W]) -> list[list[W]]:
    out: list[list[W]] = []
    for w in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0)):
        yc = (w.top + w.bottom) / 2
        if out and abs(yc - statistics.mean((x.top + x.bottom) / 2 for x in out[-1])) <= 3.0:
            out[-1].append(w)
        else:
            out.append([w])
    for ln in out:
        ln.sort(key=lambda w: w.x0)
    return out


def _line_text(ln: list[W]) -> str:
    return " ".join(w.text for w in ln)


@dataclass
class Col:
    role: str
    x0: float
    x1: float
    lo: float = 0.0     # zone by x-centre
    hi: float = 0.0
    rlo: float = 0.0    # zone by right edge (for right-aligned numbers)
    rhi: float = 0.0


def _find_header(lines: list[list[W]]) -> tuple[list[Col], int, int] | None:
    """Returns (columns, first_line_index, last_line_index) of the header band, or None."""
    def plain(ln):  # a header line carries no date / amount tokens
        return not any(looks_like_date(w.text) or looks_like_amount(w.text) for w in ln)

    for i in range(len(lines)):
        if not plain(lines[i]):
            continue
        band = [i]
        for j in (i - 1, i + 1):
            if 0 <= j < len(lines) and plain(lines[j]) and abs(
                    statistics.mean(w.top for w in lines[j]) - statistics.mean(w.top for w in lines[i])) <= 14:
                band.append(j)
        band.sort()
        words = sorted((w for j in band for w in lines[j]), key=lambda w: w.x0)
        clusters: list[list[W]] = []
        for w in words:
            if clusters and w.x0 <= max(x.x1 for x in clusters[-1]) + 5:
                clusters[-1].append(w)
            else:
                clusters.append([w])
        cols: list[Col] = []
        for cl in clusters:
            label = " ".join(x.text for x in sorted(cl, key=lambda x: (round(x.top), x.x0)))
            role = _role_of(label)
            if role:
                cols.append(Col(role, min(x.x0 for x in cl), max(x.x1 for x in cl)))
        roles = [c.role for c in cols]
        if "date" in roles and "balance" in roles and ({"debit", "credit", "amount"} & set(roles)):
            if len(set(roles) - {"drcr"}) != len([r for r in roles if r != "drcr"]):
                continue  # a role appearing twice: probably not the transaction header
            return cols, band[0], band[-1]
    return None


def _zones(cols: list[Col], page_w: float) -> None:
    cols.sort(key=lambda c: c.x0)
    for i, c in enumerate(cols):
        c.lo = 0.0 if i == 0 else (cols[i - 1].x1 + c.x0) / 2
        c.hi = page_w if i == len(cols) - 1 else (c.x1 + cols[i + 1].x0) / 2
    byr = sorted(cols, key=lambda c: c.x1)
    for i, c in enumerate(byr):
        c.rlo = 0.0 if i == 0 else (byr[i - 1].x1 + c.x1) / 2
        c.rhi = page_w if i == len(byr) - 1 else (c.x1 + byr[i + 1].x1) / 2


_NUMERIC_ROLES = {"debit", "credit", "amount", "balance"}


def _refine_zones(cols: list[Col], body: list[list[W]], page_w: float) -> None:
    """Header text is short, but the text under it can run much wider (a long narration under the word
    "Narration"). Where a text column sits next to a number column, its zone is stretched up to the left-most
    number actually printed in that column."""
    data_left: dict[str, float] = {}
    for ln in body:
        for w in ln:
            if looks_like_amount(w.text):
                b = next((c for c in cols if c.rlo <= w.x1 < c.rhi), None)
                if b is not None and b.role in _NUMERIC_ROLES:
                    data_left[b.role] = min(data_left.get(b.role, 1e9), w.x0)
    for i in range(len(cols) - 1):
        c, n = cols[i], cols[i + 1]
        if c.role in _NUMERIC_ROLES or c.role == "drcr":
            continue
        if n.role in _NUMERIC_ROLES:
            edge = min(n.x0, data_left.get(n.role, n.x0)) - 2
            if edge > c.hi:
                c.hi = edge
                n.lo = edge


def _assign_line(cols: list[Col], ln: list[W]) -> dict[str, list[W]]:
    by_col: dict[str, list[W]] = {}
    prev_role = None
    prev_w: W | None = None
    for w in ln:
        c = _assign(cols, w)
        role = c.role if c else None
        # words that run on from the narration with an ordinary word space belong to the narration
        if (role in ("ref", "value_date", None) and prev_role == "desc" and prev_w is not None
                and w.x0 - prev_w.x1 < 6 and not looks_like_date(w.text)
                and "desc" in {x.role for x in cols}):
            role = "desc"
        if role:
            by_col.setdefault(role, []).append(w)
        prev_role, prev_w = role, w
    return by_col


def _assign(cols: list[Col], w: W) -> Col | None:
    a = next((c for c in cols if c.lo <= w.xc < c.hi), None)
    if looks_like_amount(w.text):
        b = next((c for c in cols if c.rlo <= w.x1 < c.rhi), None)
        if b is not None and b.role in _NUMERIC_ROLES and (a is None or a.role in _NUMERIC_ROLES or a.role == "drcr"):
            return b
    return a


# ── row model ─────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Row:
    id: int
    page: int
    top: float
    bottom: float
    x0: float
    x1: float
    cells: dict = field(default_factory=lambda: {r: [] for r in ("date", "value_date", "desc", "ref", "debit", "credit", "amount", "balance", "drcr")})
    date_text: str = ""
    # parsed
    date: date | None = None
    debit: Decimal | None = None
    credit: Decimal | None = None
    amount: Decimal | None = None      # signed: +in, -out
    balance: Decimal | None = None
    direction_inferred: bool = False
    status: str = "unchecked"
    flags: list = field(default_factory=list)

    def flag(self, code: str, message: str, severity: str = "review"):
        if not any(f["code"] == code for f in self.flags):
            self.flags.append({"code": code, "message": message, "severity": severity})


_FOOTER = re.compile(r"(page\s+\d+|computer generated|registered office|customer care|toll free|statement of account|"
                     r"this is a system|end of statement|\bgstin\b|continued on|\bwww\.)", re.I)
_OPENING = re.compile(r"(opening\s+balance|balance\s+b/?f|brought\s+forward|balance\s+forward)", re.I)
_CLOSING = re.compile(r"(closing\s+balance|balance\s+c/?f|carried\s+forward)", re.I)
_TOTAL = re.compile(r"\btotal(s)?\b", re.I)


def _cell_text(ws: list[W]) -> str:
    return " ".join(w.text for w in sorted(ws, key=lambda w: (round(w.top), w.x0)))


# ── Reader A ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _read_pages(pdf) -> tuple[list[tuple[int, float, float, list[list[W]]]], int]:
    pages = []
    chars = 0
    for pno, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
        raw = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False, use_text_flow=False)
        ws = [W(r["text"], r["x0"], r["x1"], r["top"], r["bottom"]) for r in raw]
        chars += sum(len(w.text) for w in ws)
        pages.append((pno, float(page.width), float(page.height), _lines(ws)))
    return pages, chars


def _reader_a(pages) -> tuple[list[Row], dict]:
    rows: list[Row] = []
    info = {"opening": None, "closing": None, "printed_totals": [], "header_found": False, "roles": []}
    cols: list[Col] | None = None
    for pno, pw, ph, lines in pages:
        hdr = _find_header(lines)
        start = 0
        if hdr:
            cols, hs, he = hdr
            _zones(cols, pw)
            start = he + 1
            info["header_found"] = True
            info["roles"] = [c.role for c in cols]
        if not cols:
            continue
        body = lines[start:]
        if not body:
            continue
        _refine_zones(cols, body, pw)
        gaps = [body[i + 1][0].top - body[i][0].top for i in range(len(body) - 1)]
        pitch = statistics.median(gaps) if gaps else 12.0
        prev: Row | None = None
        for ln in body:
            by_col = _assign_line(cols, ln)
            text = _line_text(ln)
            dtext = _cell_text(by_col.get("date", []))
            top, bottom = min(w.top for w in ln), max(w.bottom for w in ln)
            if dtext and looks_like_date(dtext):
                r = Row(len(rows), pno, top, bottom, min(w.x0 for w in ln), max(w.x1 for w in ln))
                r.date_text = dtext
                for role, ws in by_col.items():
                    r.cells[role] = list(ws)
                rows.append(r)
                prev = r
                continue
            if _OPENING.search(text) and "balance" in by_col:
                v, h = parse_amount(_cell_text(by_col["balance"]))
                if v is not None:
                    info["opening"] = -v if h == "Dr" else v
                prev = None
                continue
            if _CLOSING.search(text) and "balance" in by_col:
                v, h = parse_amount(_cell_text(by_col["balance"]))
                if v is not None:
                    info["closing"] = -v if h == "Dr" else v
                prev = None
                continue
            if _TOTAL.search(text) and ("debit" in by_col or "credit" in by_col):
                d, _ = parse_amount(_cell_text(by_col.get("debit", [])))
                c, _ = parse_amount(_cell_text(by_col.get("credit", [])))
                info["printed_totals"].append((d, c))
                prev = None
                continue
            if prev is None or prev.page != pno or _FOOTER.search(text):
                prev = None if _FOOTER.search(text) else prev
                continue
            if top - prev.bottom > pitch * 1.6:
                prev = None
                continue
            # continuation of the previous transaction (wrapped description, or amounts on the second line)
            for role, ws in by_col.items():
                if role in ("desc", "ref"):
                    prev.cells[role].extend(ws)
                elif not prev.cells[role] and role in ("debit", "credit", "amount", "balance", "drcr"):
                    prev.cells[role] = list(ws)
            prev.bottom = max(prev.bottom, bottom)
            prev.x1 = max(prev.x1, max(w.x1 for w in ln))
    return rows, info


# ── parse cells into numbers ──────────────────────────────────────────────────────────────────────────────────────

def _infer_date_order(rows: list[Row], forced: str | None) -> tuple[str, bool]:
    """(order, certain)."""
    if forced in ("DMY", "MDY"):
        return forced, True
    texts = [r.date_text for r in rows]
    nums = [_RE_DATE_NUM.match(t) for t in texts]
    nums = [m for m in nums if m]
    if not nums:
        return "DMY", True   # month names / ISO: no ambiguity
    first_big = any(int(m.group(1)) > 12 for m in nums)
    second_big = any(int(m.group(2)) > 12 for m in nums)
    if first_big and not second_big:
        return "DMY", True
    if second_big and not first_big:
        return "MDY", True

    def monotone(order: str) -> bool:
        ds = [parse_date(t, order) for t in texts]
        if any(d is None for d in ds):
            return False
        return all(a <= b for a, b in zip(ds, ds[1:])) or all(a >= b for a, b in zip(ds, ds[1:]))

    dm, md = monotone("DMY"), monotone("MDY")
    if dm and not md:
        return "DMY", True
    if md and not dm:
        return "MDY", True
    return "DMY", False


def _parse_rows(rows: list[Row], order: str) -> None:
    # If any amount in a single "Amount" column carries a minus sign, brackets or Dr/Cr, the statement marks
    # direction itself: an unmarked amount then means money in. Only when nothing is marked do we work it out
    # from the balance (and say so on the row).
    marked = False
    for r in rows:
        if r.cells["amount"]:
            _, h = parse_amount(_cell_text(r.cells["amount"]))
            if h or (r.cells["drcr"] and _cell_text(r.cells["drcr"]).strip()):
                marked = True
                break
    for r in rows:
        r.date = parse_date(r.date_text, order)
        if r.date is None:
            r.flag("date", f"Date “{r.date_text}” could not be read.")
        d = c = a = None
        dh = ch = ah = None
        if r.cells["debit"]:
            d, dh = parse_amount(_cell_text(r.cells["debit"]))
            if d is None:
                r.flag("amount", f"Debit “{_cell_text(r.cells['debit'])}” could not be read as a number.")
        if r.cells["credit"]:
            c, ch = parse_amount(_cell_text(r.cells["credit"]))
            if c is None:
                r.flag("amount", f"Credit “{_cell_text(r.cells['credit'])}” could not be read as a number.")
        if r.cells["amount"]:
            a, ah = parse_amount(_cell_text(r.cells["amount"]))
            if a is None:
                r.flag("amount", f"Amount “{_cell_text(r.cells['amount'])}” could not be read as a number.")
        if r.cells["balance"]:
            b, bh = parse_amount(_cell_text(r.cells["balance"]))
            if b is None:
                r.flag("balance", f"Balance “{_cell_text(r.cells['balance'])}” could not be read as a number.")
            else:
                r.balance = -b if bh == "Dr" else b
        r.debit, r.credit = d, c
        if d is not None and c is not None and d != 0 and c != 0:
            r.flag("both", "Both a debit and a credit were read on one row.")
        if d is not None or c is not None:
            r.amount = (c or Decimal(0)) - (d or Decimal(0))
        elif a is not None:
            drcr = _cell_text(r.cells["drcr"]).strip().lower() if r.cells["drcr"] else ""
            hint = ah or ("Dr" if drcr.startswith("d") else "Cr" if drcr.startswith("c") else None)
            if hint == "Dr":
                r.amount = -a
            elif hint == "Cr":
                r.amount = a
            elif marked:
                r.amount = a
            else:
                r.amount = a   # unsigned; direction settled against the balance below
                r.direction_inferred = True


# ── Reader B: independent plain-text read ─────────────────────────────────────────────────────────────────────────

def _reader_b(pdf) -> list[dict]:
    """Per-line read with no column logic: date at the start, then the numbers at the end of the line."""
    out = []
    for pno, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
        text = page.extract_text(x_tolerance=1.5, y_tolerance=2) or ""
        for line in text.splitlines():
            toks = line.split()
            if not toks:
                continue
            head = toks[0]
            if len(toks) >= 3 and re.match(r"^\d{1,2}$", toks[0]) and re.match(r"^[A-Za-z]{3,9}[,]?$", toks[1]):
                head = f"{toks[0]} {toks[1].rstrip(',')} {toks[2]}"   # "01 Apr 2024"
            if not looks_like_date(head):
                continue
            nums = []
            for t in reversed(toks):
                if looks_like_amount(t):
                    v, h = parse_amount(t)
                    nums.append((v, h))
                elif t.lower() in ("dr", "cr", "dr.", "cr."):
                    continue
                elif nums:
                    break
            nums.reverse()
            out.append({"page": pno, "nums": nums, "text": " ".join(toks)[:90]})
    return out


# ── checks ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _eq(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= CENT


def _chain_score(rows: list[Row], reverse: bool) -> int:
    seq = rows[::-1] if reverse else rows
    ok = 0
    for p, c in zip(seq, seq[1:]):
        if p.balance is None or c.balance is None or c.amount is None:
            continue
        if _eq(p.balance + c.amount, c.balance) or (c.direction_inferred and _eq(p.balance - c.amount, c.balance)):
            ok += 1
    return ok


def _diagnose(prev: Row, cur: Row, diff: Decimal, next_ok: bool | None) -> None:
    """diff = printed balance - (previous balance + amount)."""
    msg = f"Balance does not follow: previous balance {prev.balance:,.2f} and this amount {cur.amount:,.2f} give " \
          f"{prev.balance + cur.amount:,.2f}, but the statement prints {cur.balance:,.2f}."
    code = "balance_break"
    hints = []
    if cur.amount is not None and _eq(prev.balance - cur.amount, cur.balance):
        hints.append("Debit and credit look swapped on this row.")
        code = "sign"
    elif int((abs(diff) * 100).to_integral_value()) % 9 == 0 and diff != 0:
        hints.append("The gap is a multiple of 9, which is what two swapped digits produce.")
        code = "digits"
    if prev.page != cur.page:
        hints.append("This is the first row after a page break, so a row may have been lost there.")
    if cur.amount is not None and prev.date_text == cur.date_text and prev.amount == cur.amount and prev.balance == cur.balance:
        hints.append("This row is identical to the one above it; it may be a duplicate.")
        code = "duplicate"
    if next_ok is True:
        hints.append("The next row follows from this row's balance, so the amount on this row is the likely problem.")
    elif next_ok is False:
        hints.append("The next row also fails, so this row's balance may be the problem.")
    cur.flag(code, " ".join([msg] + hints))


def verify(rows: list[Row], info: dict, reader_b: list[dict], date_certain: bool, date_order: str) -> dict:
    file_flags: list[dict] = []
    has_balance = [r for r in rows if r.balance is not None]

    # 1. direction + chain
    if len(has_balance) >= 2:
        fwd, rev = _chain_score(rows, False), _chain_score(rows, True)
        reverse = rev > fwd
        seq = rows[::-1] if reverse else rows
        for r in rows:
            if r.direction_inferred and r.amount is not None:
                pass
        # settle unsigned amounts from the balance change
        for p, c in zip(seq, seq[1:]):
            if c.direction_inferred and c.amount is not None and p.balance is not None and c.balance is not None:
                if _eq(p.balance + c.amount, c.balance):
                    pass
                elif _eq(p.balance - c.amount, c.balance):
                    c.amount = -c.amount
                c.flag("inferred", "The statement does not mark this amount as debit or credit; the direction was "
                                   "worked out from the change in balance.", "info")
        passes: dict[int, bool | None] = {}
        for i in range(1, len(seq)):
            p, c = seq[i - 1], seq[i]
            if p.balance is None or c.balance is None or c.amount is None:
                passes[c.id] = None
                continue
            passes[c.id] = _eq(p.balance + c.amount, c.balance)
        for i in range(1, len(seq)):
            p, c = seq[i - 1], seq[i]
            ok = passes.get(c.id)
            if ok is None:
                continue
            if ok:
                c.status = "verified"
            else:
                nxt = passes.get(seq[i + 1].id) if i + 1 < len(seq) else None
                _diagnose(p, c, c.balance - (p.balance + c.amount), nxt)
                c.status = "flagged"
        first = seq[0]
        if first.balance is not None and first.amount is not None:
            if info.get("opening") is not None:
                exp = first.balance - first.amount
                if _eq(info["opening"], exp):
                    first.status = "verified"
                else:
                    first.flag("opening", f"Opening balance {info['opening']:,.2f} plus this amount does not give the "
                                          f"printed balance {first.balance:,.2f}.")
                    first.status = "flagged"
            else:
                first.status = "unchecked"
                first.flag("first_row", "This is the first row and there is no opening balance printed, so it cannot be "
                                        "checked against a row before it.", "info")
    else:
        file_flags.append({"code": "no_balances", "severity": "review",
                           "message": "This statement does not print a running balance, so rows cannot be proven. "
                                      "Check the totals against the PDF yourself."})

    # 2. second reader
    if reader_b:
        if len(reader_b) != len(rows):
            # Which lines does only the second reader see? Match balances one to one and list what is left over.
            pool = [r.balance for r in rows if r.balance is not None]
            extra = []
            for b in reader_b:
                v = b["nums"][-1][0] if b.get("nums") else None
                hit = next((i for i, x in enumerate(pool) if v is not None and _eq(abs(x), abs(v))), None)
                if hit is None:
                    extra.append(b.get("text") or "")
                else:
                    pool.pop(hit)
            sample = "; ".join(f"\"{e}\"" for e in extra[:2] if e)
            bad_rows = any(r.status == "flagged" for r in rows)
            unproven = sum(1 for r in rows if r.status != "verified")
            chain_ok = len(has_balance) == len(rows) and not bad_rows and unproven <= 1
            if len(reader_b) > len(rows) and chain_ok:
                # A transaction missing from the main read would break the balance chain, and it did not,
                # so the extra lines are most likely headers or notes that happen to start with a date.
                file_flags.append({"code": "row_count", "severity": "info",
                                   "message": f"The second reader counted {len(reader_b) - len(rows)} more dated line(s) than the "
                                              f"main reader" + (f" ({sample})" if sample else "") + ". Every row the main reader "
                                              "found follows from the balance before it, so a missing transaction would have "
                                              "broken the chain. These are most likely headings or notes, not transactions."})
            else:
                file_flags.append({"code": "row_count", "severity": "review",
                                   "message": f"The main reader found {len(rows)} transactions but the second reader found "
                                              f"{len(reader_b)}. Rows may have been merged or lost."
                                              + (f" Lines only the second reader saw: {sample}." if sample else "")})
        else:
            for r, b in zip(rows, reader_b):
                nums = [v for v, _ in b["nums"]]
                mine = [x for x in (r.balance, None if r.amount is None else abs(r.amount)) if x is not None]
                if not nums or not mine:
                    continue
                bal_b = nums[-1]
                amt_b = nums[-2] if len(nums) >= 2 else None
                bad = []
                if r.balance is not None and not _eq(abs(r.balance), bal_b):
                    bad.append(f"balance {r.balance:,.2f} vs {bal_b:,.2f}")
                if r.amount is not None and amt_b is not None and not _eq(abs(r.amount), amt_b):
                    bad.append(f"amount {abs(r.amount):,.2f} vs {amt_b:,.2f}")
                if bad:
                    r.flag("readers_disagree", "The two readers disagree on this row (" + "; ".join(bad) + ").")
                    r.status = "flagged"

    # 3. whole-file totals
    amts = [r.amount for r in rows if r.amount is not None]
    if rows and has_balance and amts:
        seq0 = rows[::-1] if (len(has_balance) >= 2 and _chain_score(rows, True) > _chain_score(rows, False)) else rows
        first, last = seq0[0], seq0[-1]
        if info.get("closing") is not None and last.balance is not None and not _eq(info["closing"], last.balance):
            file_flags.append({"code": "closing", "severity": "review",
                               "message": f"The closing balance printed on the statement is {info['closing']:,.2f}, "
                                          f"but the last row ends at {last.balance:,.2f}. A row may be missing."})
        if first.balance is not None and first.amount is not None and last.balance is not None:
            start = info["opening"] if info.get("opening") is not None else first.balance - first.amount
            if not _eq(start + sum(amts), last.balance):
                file_flags.append({"code": "total", "severity": "review",
                                   "message": f"Opening {start:,.2f} plus all rows is {start + sum(amts):,.2f}, "
                                              f"but the final balance is {last.balance:,.2f}."})
    totals = info.get("printed_totals") or []
    if len(totals) == 1:
        pd_, pc_ = totals[0]
        sd = sum((r.debit or Decimal(0)) for r in rows) if any(r.debit is not None for r in rows) else None
        sc = sum((r.credit or Decimal(0)) for r in rows) if any(r.credit is not None for r in rows) else None
        if pd_ is not None and sd is not None and not _eq(pd_, sd):
            file_flags.append({"code": "total_debit", "severity": "review",
                               "message": f"Printed total debits are {pd_:,.2f}; the rows add up to {sd:,.2f}."})
        if pc_ is not None and sc is not None and not _eq(pc_, sc):
            file_flags.append({"code": "total_credit", "severity": "review",
                               "message": f"Printed total credits are {pc_:,.2f}; the rows add up to {sc:,.2f}."})

    # 4. dates
    if not date_certain:
        file_flags.append({"code": "date_order", "severity": "review",
                           "message": "Dates like 03/04/2024 could be 3 April or 4 March and the data does not settle "
                                      "it. Pick the right order and run it again."})
    ds = [r.date for r in rows if r.date]
    if len(ds) >= 3 and not (all(a <= b for a, b in zip(ds, ds[1:])) or all(a >= b for a, b in zip(ds, ds[1:]))):
        file_flags.append({"code": "date_sequence", "severity": "review",
                           "message": "Dates are not in order from top to bottom. A date may have been misread."})

    # 5. duplicates without balances
    # (rows with identical date, description, amount and balance can be real; only flagged when a balance proves it)

    # final per-row status
    for r in rows:
        if any(f["severity"] == "review" for f in r.flags):
            r.status = "flagged"
        elif r.status != "verified":
            r.status = "unchecked"

    n_ver = sum(1 for r in rows if r.status == "verified")
    n_flag = sum(1 for r in rows if r.status == "flagged")
    n_unc = len(rows) - n_ver - n_flag
    file_review = any(f["severity"] == "review" for f in file_flags)
    if not rows:
        label = "empty"
    elif n_flag or file_review:
        label = "needs_review"
    elif n_unc == 0:
        label = "verified"
    elif n_ver == 0:
        label = "unverified"
    else:
        label = "partly_verified"
    return {"label": label, "rows": len(rows), "verified": n_ver, "flagged": n_flag, "unchecked": n_unc,
            "fileFlags": file_flags}


# ── public entry point ────────────────────────────────────────────────────────────────────────────────────────────

def _fmt(d: Decimal | None) -> str | None:
    return None if d is None else f"{d:.2f}"


def extract_statement(data: bytes, date_order: str | None = None, with_images: bool = True) -> dict:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:  # corrupt / encrypted
        return {"ok": False, "code": "unreadable", "error": f"This PDF could not be opened ({type(e).__name__}). "
                                                              "If it is password-protected, remove the password first."}
    with pdf:
        if len(pdf.pages) == 0:
            return {"ok": False, "code": "unreadable", "error": "This PDF has no pages."}
        pages, chars = _read_pages(pdf)
        if chars < 40:
            return {"ok": False, "code": "scanned",
                    "error": "This looks like a scanned or photographed statement: it has no text to read. Scanned files "
                             "cannot be checked reliably yet, so they are not guessed. Download the PDF from your bank's "
                             "website or app instead of scanning it."}
        rows, info = _reader_a(pages)
        if not info["header_found"]:
            return {"ok": False, "code": "no_table",
                    "error": "No transaction table was found. Statements need a header row with a date column, a balance "
                             "column and a debit/credit or amount column."}
        order, certain = _infer_date_order(rows, date_order)
        _parse_rows(rows, order)
        rb = _reader_b(pdf)
        result = verify(rows, info, rb, certain, order)

        images = {}
        if with_images:
            for pno, _w, _h, _l in pages:
                try:
                    im = pdf.pages[pno - 1].to_image(resolution=84).original.convert("RGB")
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=70)
                    images[pno] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                except Exception:
                    images[pno] = None
        truncated = len(pdf.pages) > MAX_PAGES

    out_rows = []
    for r in rows:
        out_rows.append({
            "id": r.id, "page": r.page,
            "bbox": [round(r.x0, 1), round(r.top, 1), round(r.x1, 1), round(r.bottom, 1)],
            "date": r.date.isoformat() if r.date else r.date_text,
            "description": _cell_text(r.cells["desc"]),
            "ref": _cell_text(r.cells["ref"]),
            "debit": _fmt(-r.amount if r.amount is not None and r.amount < 0 else None),
            "credit": _fmt(r.amount if r.amount is not None and r.amount > 0 else None),
            "balance": _fmt(r.balance),
            "status": r.status, "flags": r.flags,
        })
    return {
        "ok": True, "summary": result, "dateOrder": order, "dateOrderCertain": certain,
        "pages": [{"page": p, "width": w, "height": h, "image": images.get(p)} for p, w, h, _ in pages],
        "rows": out_rows, "truncated": truncated,
        "readers": "Two independent reads of the PDF's text, plus the statement's own balances.",
    }
