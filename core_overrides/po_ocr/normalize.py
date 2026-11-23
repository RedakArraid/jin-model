from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata

CURRENCY_SYMBOLS = {
    "€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY", "CHF": "CHF",
}
KNOWN_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "JPY", "CNY", "CAD", "AUD", "SEK", "NOK", "DKK", "PLN", "CZK"}
UNIT_MAP = {
    "PCS": "EA", "PC": "EA", "PCE": "EA", "PIECE": "EA", "PIECES": "EA",
    "EA": "EA", "UNIT": "EA", "UNITS": "EA", "STK": "EA", "STÜCK": "EA", "STUCK": "EA",
    "KG": "KG", "G": "G", "L": "L", "M": "M", "M2": "M2", "M3": "M3",
    "BOX": "BOX", "BX": "BOX", "PAL": "PAL", "PALLET": "PAL",
}


def ascii_fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"\s+", " ", text).strip()


def normalize_currency(text: str | None, default: str | None = None) -> str | None:
    if not text:
        return default
    upper = text.upper()
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    for code in KNOWN_CURRENCIES:
        if re.search(rf"\b{re.escape(code)}\b", upper):
            return code
    return default


def normalize_unit(text: str | None) -> str | None:
    if not text:
        return None
    key = ascii_fold(text).upper().strip().strip(".,;:")
    return UNIT_MAP.get(key, key)


def parse_number(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.replace(" ", " ")
    s = re.sub(r"[^0-9,.'+\- ]", "", s)
    s = s.replace("'", "").replace(" ", "")
    if not s or s in {"+", "-"}:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) in (1, 2, 3):
            s = "".join(parts[:-1]).replace(",", "") + "." + parts[-1]
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2:
            if len(parts[-1]) in (1, 2):
                s = "".join(parts[:-1]) + "." + parts[-1]
            else:
                s = "".join(parts)
    try:
        result = float(Decimal(s))
        return -result if negative else result
    except (InvalidOperation, ValueError):
        return None


_DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y",
    "%d-%m-%Y", "%d-%m-%y", "%m/%d/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
]

MONTHS = {
    "janvier": "January", "fevrier": "February", "février": "February", "mars": "March", "avril": "April",
    "mai": "May", "juin": "June", "juillet": "July", "aout": "August", "août": "August",
    "septembre": "September", "octobre": "October", "novembre": "November", "decembre": "December", "décembre": "December",
    "januar": "January", "februar": "February", "marz": "March", "märz": "March", "mai": "May", "juni": "June",
    "juli": "July", "august": "August", "september": "September", "oktober": "October", "november": "November", "dezember": "December",
}


def parse_date(value: str | None, date_order: str = "DMY") -> str | None:
    if not value:
        return None
    s = clean_text(value) or ""
    s = re.sub(r"^(date|datum|fecha)\s*[:\-]?\s*", "", s, flags=re.I)
    for src, dst in MONTHS.items():
        s = re.sub(rf"\b{src}\b", dst, s, flags=re.I)
    candidates = [s]
    m = re.search(r"\b(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})\b", s)
    if m:
        candidates.insert(0, m.group(1))
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                pass
    nums = re.findall(r"\d+", s)
    if len(nums) == 3:
        try:
            a, b, c = map(int, nums)
            if a > 999:
                y, mth, d = a, b, c
            elif c > 999:
                y = c
                if date_order.upper() == "MDY":
                    mth, d = a, b
                else:
                    d, mth = a, b
            else:
                return None
            return datetime(y, mth, d).date().isoformat()
        except (ValueError, OverflowError):
            return None
    return None
