from __future__ import annotations

import re
import unicodedata


def ascii_fold(value: str) -> str:
    text = str(value or "")
    text = (
        text.replace("œ", "oe")
        .replace("Œ", "OE")
        .replace("æ", "ae")
        .replace("Æ", "AE")
    )
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )


def clean_text(value: str) -> str:
    text = str(value or "").replace("\u00a0", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", text).strip()
