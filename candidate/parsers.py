"""
Level 1 — Parsing utilities for Italian procurement data.

Hints:
- Italian locale uses '.' as thousands separator and ',' as decimal separator
- Italian dates can appear as DD/MM/YYYY, "DD mese YYYY", or ISO YYYY-MM-DD
- A valid CIG is exactly 10 alphanumeric characters and is NOT a placeholder
"""
from __future__ import annotations
from datetime import date
import re


_MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

_PLACEHOLDER_CIGS = {
    "0000000000",
    "0000000001",
    "XXXXXXXXXX",
}


def parse_amount(raw: str) -> float | None:
    """Parse an Italian-format monetary amount into a float. Returns None if unparseable."""
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    text = (
        text.replace("\xa0", " ")
        .replace("\u202f", " ")
        .replace("€", " ")
        .replace("EUR", " ")
        .replace("eur", " ")
    )
    match = re.search(r"[-+]?\d[\d\s.,]*", text)
    if not match:
        return None

    value = re.sub(r"\s+", "", match.group(0))
    if not value or not re.search(r"\d", value):
        return None

    comma_pos = value.rfind(",")
    dot_pos = value.rfind(".")

    if comma_pos >= 0 and dot_pos >= 0:
        if comma_pos > dot_pos:
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif comma_pos >= 0:
        value = value.replace(".", "").replace(",", ".")
    elif dot_pos >= 0:
        dot_count = value.count(".")
        decimals = len(value) - dot_pos - 1
        if dot_count > 1 or decimals == 3:
            value = value.replace(".", "")

    try:
        return float(value)
    except ValueError:
        return None


def parse_date(raw: str) -> date | None:
    """Parse an Italian-format date string into a datetime.date. Returns None if unparseable."""
    if raw is None:
        return None

    text = re.sub(r"\s+", " ", str(raw).strip().lower())
    if not text:
        return None

    iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_match:
        try:
            return date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
        except ValueError:
            return None

    numeric_match = re.search(r"\b(\d{1,2})[/. -](\d{1,2})[/. -](\d{4})\b", text)
    if numeric_match:
        try:
            return date(
                int(numeric_match.group(3)),
                int(numeric_match.group(2)),
                int(numeric_match.group(1)),
            )
        except ValueError:
            return None

    month_names = "|".join(_MONTHS)
    textual_match = re.search(
        rf"\b(\d{{1,2}})\s+({month_names})\s+(\d{{4}})\b",
        text,
    )
    if textual_match:
        try:
            return date(
                int(textual_match.group(3)),
                _MONTHS[textual_match.group(2)],
                int(textual_match.group(1)),
            )
        except ValueError:
            return None

    return None


def is_valid_cig(cig: str | None) -> bool:
    """Check whether a CIG is valid (10 alnum, not a placeholder)."""
    if cig is None:
        return False

    normalized = str(cig).strip().upper()
    if len(normalized) != 10 or not normalized.isalnum():
        return False
    if normalized in _PLACEHOLDER_CIGS:
        return False

    return True
