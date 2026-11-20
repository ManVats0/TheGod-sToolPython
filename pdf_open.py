"""Open a PDF for reading, with an optional password.

The password is used only to open the file in memory. It is never stored or logged, and the PDF is not saved.
Returns (pdf, None) on success, or (None, error_dict) with a code the website can act on:
  password_required  the PDF is locked and no password was given
  password_wrong     a password was given but it does not open the PDF
  unreadable         the file is damaged or not a PDF we can open
"""
from __future__ import annotations

import io

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect


def open_pdf(data: bytes, password: str | None = None):
    try:
        return pdfplumber.open(io.BytesIO(data), password=password or None), None
    except Exception as e:
        # pdfplumber wraps the reader's own errors; look inside to find a password problem
        inner = e.args[0] if getattr(e, "args", None) else e
        if not isinstance(e, PDFPasswordIncorrect) and not isinstance(inner, PDFPasswordIncorrect):
            return None, {"ok": False, "code": "unreadable",
                          "error": f"This PDF could not be opened ({type(inner).__name__})."}
        if password:
            return None, {"ok": False, "code": "password_wrong",
                          "error": "That password did not open the PDF. Check it and try again."}
        return None, {"ok": False, "code": "password_required",
                      "error": "This PDF is password-protected. Enter its password to read it. "
                               "The password is used only to open the file and is not stored."}
