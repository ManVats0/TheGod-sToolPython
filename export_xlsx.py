"""Rows -> a formatted .xlsx file, built from what the person sees on screen (including any edits they made).

Dates become real Excel dates and amounts become real numbers, so the sheet sorts and adds up. Text is always stored as
text, even if it starts with "=", so a cell can never run as a formula.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

MAX_ROWS = 50_000
MAX_COLS = 40
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _number(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return float(-d if neg else d)


def _date(v):
    if isinstance(v, str) and _ISO.match(v.strip()):
        try:
            return datetime.strptime(v.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def build_workbook(columns: list[dict], rows: list[list], sheet: str = "Data") -> bytes:
    columns = columns[:MAX_COLS]
    rows = rows[:MAX_ROWS]
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]\*\?/\\:]", " ", (sheet or "Data"))[:31] or "Data"
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    for c, col in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c, value=str(col.get("name", ""))[:100])
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = head_fill
    widths = [len(str(col.get("name", ""))) + 2 for col in columns]
    for r, row in enumerate(rows, start=2):
        for c, col in enumerate(columns, start=1):
            raw = row[c - 1] if c - 1 < len(row) else None
            kind = col.get("type", "text")
            cell = ws.cell(row=r, column=c)
            cell.font = Font(name="Arial", size=10)
            if kind == "number" and _number(raw) is not None:
                cell.value = _number(raw)
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")
                width = 14
            elif kind == "date" and _date(raw) is not None:
                cell.value = _date(raw)
                cell.number_format = "yyyy-mm-dd"
                width = 12
            else:
                text = "" if raw is None else str(raw)
                cell.value = text
                cell.data_type = "s"          # never a formula
                width = min(len(text), 60)
            widths[c - 1] = max(widths[c - 1], min(width, 60))
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = max(w + 2, 8)
    ws.freeze_panes = "A2"
    if columns and rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
