"""Read-only verification against a compact local Base Adresse Nationale index."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable


RECORD_BYTES = 16
STREET_TYPES = {
    "AV": "AVENUE", "AV.": "AVENUE", "BD": "BOULEVARD", "BD.": "BOULEVARD",
    "RTE": "ROUTE", "CHE": "CHEMIN", "CH": "CHEMIN", "IMP": "IMPASSE",
    "PL": "PLACE", "ALL": "ALLEE", "ALL.": "ALLEE", "ZA": "ZONE ACTIVITE",
    "ZAC": "ZONE ACTIVITE", "ZI": "ZONE INDUSTRIELLE",
}


def normalize_part(value: Any) -> str:
    text = "".join(char for char in unicodedata.normalize("NFKD", str(value or ""))
                   if not unicodedata.combining(char)).upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text).strip()
    tokens = text.split()
    if tokens and tokens[0] in STREET_TYPES:
        tokens[0] = STREET_TYPES[tokens[0]]
    return " ".join(tokens)


def normalize_number(value: Any) -> str:
    text = normalize_part(value)
    return str(int(text)) if text.isdigit() else text


def canonical_address_key(*, number: Any, repetition: Any, street: Any,
                          postal_code: Any, city: Any) -> bytes | None:
    parts = (
        normalize_number(number), normalize_part(repetition), normalize_part(street),
        normalize_part(postal_code), normalize_part(city),
    )
    if not parts[0] or not parts[2] or not re.fullmatch(r"\d{5}", parts[3]) or not parts[4]:
        return None
    return hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=RECORD_BYTES).digest()


def key_from_ban_row(row: dict[str, Any]) -> bytes | None:
    return canonical_address_key(
        number=row.get("numero"), repetition=row.get("rep"),
        street=row.get("nom_voie") or row.get("nom_afnor"),
        postal_code=row.get("code_postal"), city=row.get("nom_commune"),
    )


def key_from_extracted(address: dict[str, Any]) -> bytes | None:
    street = address.get("street")
    if not street:
        street = " ".join(str(value) for value in
                          (address.get("street_type"), address.get("street_name")) if value)
    return canonical_address_key(
        number=address.get("house_number") or address.get("building_number"),
        repetition=address.get("house_number_suffix"), street=street,
        postal_code=address.get("postal_code"), city=address.get("city"),
    )


def department_candidates(postal_code: Any) -> list[str]:
    value = re.sub(r"\D", "", str(postal_code or ""))
    if len(value) != 5:
        return []
    if value.startswith("20"):
        return ["2A", "2B"]
    if value.startswith(("97", "98")):
        return [value[:3]]
    return [value[:2]]


def write_sorted_index(keys: Iterable[bytes], destination: Path) -> dict[str, Any]:
    unique = sorted({key for key in keys if isinstance(key, bytes) and len(key) == RECORD_BYTES})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("wb") as handle:
        for key in unique:
            handle.write(key)
    os.replace(temporary, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {"records": len(unique), "bytes": destination.stat().st_size, "sha256": digest}


def index_contains(path: Path, key: bytes) -> bool:
    if len(key) != RECORD_BYTES or not path.is_file() or path.stat().st_size % RECORD_BYTES:
        return False
    with path.open("rb") as handle:
        low, high = 0, path.stat().st_size // RECORD_BYTES
        while low < high:
            middle = (low + high) // 2
            handle.seek(middle * RECORD_BYTES)
            candidate = handle.read(RECORD_BYTES)
            if candidate < key:
                low = middle + 1
            else:
                high = middle
        handle.seek(low * RECORD_BYTES)
        return handle.read(RECORD_BYTES) == key


class LocalBanReference:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory or os.getenv("JIN_BAN_INDEX_DIR", "/app/data/reference/ban"))
        self.manifest_path = self.directory / "manifest.json"
        self.manifest: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.manifest = {}
        try:
            self._manifest_mtime = self.manifest_path.stat().st_mtime_ns
        except OSError:
            self._manifest_mtime = None

    def _reload_if_changed(self) -> None:
        try:
            modified = self.manifest_path.stat().st_mtime_ns
        except OSError:
            modified = None
        if modified != getattr(self, "_manifest_mtime", None):
            self.reload()

    @property
    def available(self) -> bool:
        return bool(self.manifest.get("departments"))

    def status(self) -> dict[str, Any]:
        self._reload_if_changed()
        departments = self.manifest.get("departments") or {}
        return {
            "available": self.available, "provider": "Base Adresse Nationale",
            "mode": "local_exact_and_street_index", "directory": str(self.directory),
            "dataset_date": self.manifest.get("dataset_date"),
            "departments_indexed": len(departments),
            "records": sum(int(item.get("records", 0)) for item in departments.values()),
            "street_records": sum(int(item.get("street_records", 0)) for item in departments.values()),
            "license": self.manifest.get("license", "Licence Ouverte 2.0"),
        }

    def _street_match(self, department: str, address: dict[str, Any]) -> dict[str, Any] | None:
        path = self.directory / f"streets-{department}.sqlite"
        postal = normalize_part(address.get("postal_code"))
        city = normalize_part(address.get("city"))
        street = address.get("street") or " ".join(str(value) for value in
                    (address.get("street_type"), address.get("street_name")) if value)
        street = normalize_part(street)
        if not path.is_file() or not postal or not city or not street:
            return None
        database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = database.execute(
                "SELECT city_norm, street_norm, city, street FROM streets WHERE postal_code = ?",
                (postal,),
            ).fetchall()
        finally:
            database.close()
        if not rows:
            return None
        ranked = []
        for city_norm, street_norm, city_display, street_display in rows:
            city_equivalent = (city == city_norm or city.startswith(city_norm + " ")
                               or city_norm.startswith(city + " "))
            city_score = 1.0 if city_equivalent else SequenceMatcher(None, city, city_norm).ratio()
            street_score = SequenceMatcher(None, street, street_norm).ratio()
            score = 0.35 * city_score + 0.65 * street_score
            ranked.append((score, city_score, street_score, city_display, street_display,
                           city_equivalent))
        ranked.sort(reverse=True)
        best = ranked[0]
        margin = best[0] - ranked[1][0] if len(ranked) > 1 else best[0]
        exact = best[5] and best[2] == 1.0
        if not exact and (best[0] < 0.92 or margin < 0.04):
            return None
        return {
            "status": "STREET_MATCH" if exact else "CLOSE_STREET_MATCH",
            "score": round(best[0], 4), "margin": round(margin, 4),
            "suggested_components": {"city": best[3], "street": best[4]},
            "requires_review": not exact,
        }

    def verify(self, block: dict[str, Any]) -> dict[str, Any]:
        self._reload_if_changed()
        address = block.get("address") if isinstance(block.get("address"), dict) else block
        postal = address.get("postal_code")
        departments = department_candidates(postal)
        key = key_from_extracted(address)
        base = {
            "provider": "Base Adresse Nationale", "mode": "local_exact_and_street_index",
            "dataset_date": self.manifest.get("dataset_date"),
            "license": self.manifest.get("license", "Licence Ouverte 2.0"),
            "departments_checked": departments,
        }
        country = normalize_part(address.get("country_code") or address.get("country"))
        if country and country not in {"FR", "FRA", "FRANCE"}:
            return {**base, "status": "NOT_APPLICABLE", "verified": False,
                    "conclusive": False}
        indexed = [code for code in departments if (self.directory / f"adresses-{code}.idx").is_file()]
        if not indexed:
            return {**base, "status": "DEPARTMENT_NOT_INDEXED", "verified": False,
                    "conclusive": False}
        if key is not None and any(index_contains(self.directory / f"adresses-{code}.idx", key) for code in indexed):
            street_match = next((match for code in indexed
                                 if (match := self._street_match(code, address)) is not None), None)
            matched = (street_match or {}).get("suggested_components") or {}
            return {**base, "status": "EXACT_MATCH", "verified": True, "conclusive": True,
                    "matched_components": {
                        "house_number": address.get("house_number") or address.get("building_number"),
                        "postal_code": address.get("postal_code"), **matched,
                    }}
        street_match = next((match for code in indexed
                             if (match := self._street_match(code, address)) is not None), None)
        if street_match:
            status = street_match["status"]
            suggested = street_match.get("suggested_components") or {}
            canonical_key = canonical_address_key(
                number=address.get("house_number") or address.get("building_number"),
                repetition=address.get("house_number_suffix"),
                street=suggested.get("street"), postal_code=address.get("postal_code"),
                city=suggested.get("city"),
            )
            if canonical_key is not None and any(
                    index_contains(self.directory / f"adresses-{code}.idx", canonical_key)
                    for code in indexed):
                return {
                    **base, **street_match, "status": "CANONICAL_MATCH",
                    "verified": True, "conclusive": True, "requires_review": False,
                    "matched_components": {
                        "house_number": address.get("house_number") or address.get("building_number"),
                        "postal_code": address.get("postal_code"), **suggested,
                    },
                }
            if status == "STREET_MATCH" and key is not None:
                status = "STREET_MATCH_NUMBER_NOT_FOUND"
            return {**base, **street_match, "status": status, "verified": False,
                    "conclusive": False}
        if key is None:
            return {**base, "status": "INSUFFICIENT_COMPONENTS", "verified": False,
                    "conclusive": False}
        # Absence is not proof that a postal address is invalid: CEDEX, BP, new
        # or incomplete addresses may legitimately be absent from this index.
        return {**base, "status": "NOT_FOUND", "verified": False, "conclusive": False}

    def enrich(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reload_if_changed()
        if not self.available:
            payload.setdefault("reference_data", {})["ban"] = self.status()
            return payload
        seen: set[int] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"business_addresses", "addresses"} and isinstance(child, list):
                        for block in child:
                            if not isinstance(block, dict) or id(block) in seen:
                                continue
                            seen.add(id(block))
                            result = self.verify(block)
                            block["ban_verification"] = result
                            if result["status"] in {"EXACT_MATCH", "CANONICAL_MATCH"}:
                                block.update(
                                    validation_status="VERIFIED", validation_score=1.0,
                                    is_verified_real_address=True,
                                    verification_provider="Base Adresse Nationale (local)",
                                )
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        payload.setdefault("reference_data", {})["ban"] = self.status()
        return payload
