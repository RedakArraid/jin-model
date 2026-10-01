#!/usr/bin/env python3
"""Build a compact, resumable local exact-match index from official BAN CSV files."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jin_runtime.ban_reference import key_from_ban_row, write_sorted_index


ALL_DEPARTMENTS = (
    [f"{number:02d}" for number in range(1, 20)] + ["2A", "2B"]
    + [f"{number:02d}" for number in range(21, 96)]
    + ["971", "972", "973", "974", "975", "976", "977", "978", "984", "986", "987", "988", "989"]
)
BASE_URL = "https://adresse.data.gouv.fr/data/ban/adresses/latest/csv"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_streets(rows: set[tuple[str, str, str, str, str]], destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=destination.name + ".", suffix=".tmp",
                                     dir=destination.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        database = sqlite3.connect(temporary_path)
        try:
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA synchronous=OFF")
            database.execute("CREATE TABLE streets (postal_code TEXT NOT NULL, city_norm TEXT NOT NULL, "
                             "street_norm TEXT NOT NULL, city TEXT NOT NULL, street TEXT NOT NULL, "
                             "PRIMARY KEY (postal_code, city_norm, street_norm)) WITHOUT ROWID")
            database.executemany("INSERT OR IGNORE INTO streets VALUES (?, ?, ?, ?, ?)", sorted(rows))
            database.execute("CREATE INDEX streets_postal ON streets(postal_code)")
            database.commit()
        finally:
            database.close()
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"street_records": len(rows), "street_bytes": destination.stat().st_size,
            "street_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def build_from_csv(stream, destination: Path, streets_destination: Path | None = None) -> dict:
    from jin_runtime.ban_reference import normalize_part

    reader = csv.DictReader(stream, delimiter=";")
    required = {"numero", "rep", "nom_voie", "code_postal", "nom_commune"}
    if reader.fieldnames is None:
        result = write_sorted_index([], destination)
        result["source_rows"] = 0
        if streets_destination is not None:
            result.update(_write_streets(set(), streets_destination))
        return result
    if not required.issubset(reader.fieldnames):
        raise ValueError(f"BAN CSV columns missing: {sorted(required - set(reader.fieldnames or []))}")
    rows = 0
    keys = set()
    streets = set()
    for row in reader:
        rows += 1
        key = key_from_ban_row(row)
        if key is not None:
            keys.add(key)
        postal, city, street = row.get("code_postal"), row.get("nom_commune"), row.get("nom_voie")
        if re.fullmatch(r"\d{5}", str(postal or "")) and city and street:
            streets.add((str(postal), normalize_part(city), normalize_part(street),
                         str(city).strip(), str(street).strip()))
    result = write_sorted_index(keys, destination)
    result.update(source_rows=rows)
    if streets_destination is not None:
        result.update(_write_streets(streets, streets_destination))
    return result


def download_department(code: str, destination: Path, streets_destination: Path,
                        timeout: int = 120) -> dict:
    url = f"{BASE_URL}/adresses-{code}.csv.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "JIN-local-BAN-index/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        headers = response.headers
        with gzip.GzipFile(fileobj=response) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8-sig", newline="") as text:
                result = build_from_csv(text, destination, streets_destination)
        result.update(source_url=url, etag=headers.get("ETag"),
                      last_modified=headers.get("Last-Modified"))
        return result


def parse_departments(value: str) -> list[str]:
    requested = [item.strip().upper() for item in value.split(",") if item.strip()]
    invalid = [item for item in requested if item not in ALL_DEPARTMENTS]
    if invalid:
        raise ValueError(f"Unknown department codes: {', '.join(invalid)}")
    return list(dict.fromkeys(requested))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--all", action="store_true", help="Index all current BAN department files")
    choice.add_argument("--departments", help="Comma-separated department codes, e.g. 01,06,2A,971")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "reference" / "ban")
    parser.add_argument("--force", action="store_true", help="Rebuild already indexed departments")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    selected = list(ALL_DEPARTMENTS) if args.all else parse_departments(args.departments)
    output = args.output.resolve()
    manifest_path = output / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    manifest.update(
        schema_version="jin-ban-index-v1", provider="Base Adresse Nationale",
        source=BASE_URL, license="Licence Ouverte 2.0",
        license_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    )
    departments = manifest.setdefault("departments", {})
    failures = []
    for position, code in enumerate(selected, 1):
        destination = output / f"adresses-{code}.idx"
        streets_destination = output / f"streets-{code}.sqlite"
        if destination.is_file() and streets_destination.is_file() and code in departments and not args.force:
            print(f"[{position}/{len(selected)}] {code}: already indexed", flush=True)
            continue
        print(f"[{position}/{len(selected)}] {code}: downloading and indexing", flush=True)
        try:
            info = download_department(code, destination, streets_destination, timeout=args.timeout)
            departments[code] = info
            dates = [item.get("last_modified") for item in departments.values() if item.get("last_modified")]
            manifest["dataset_date"] = max(dates) if dates else None
            manifest["built_at"] = datetime.now(timezone.utc).isoformat()
            manifest["records"] = sum(int(item.get("records", 0)) for item in departments.values())
            _atomic_json(manifest_path, manifest)
            print(f"[{position}/{len(selected)}] {code}: {info['records']} records, {info['bytes']} bytes", flush=True)
        except Exception as exc:
            detail = str(exc).replace("\r", " ").replace("\n", " ")[:300]
            failures.append({"department": code, "error": type(exc).__name__, "detail": detail})
            print(f"[{position}/{len(selected)}] {code}: ERROR {type(exc).__name__}: {detail}", flush=True)
    if failures:
        manifest["failures"] = failures
        _atomic_json(manifest_path, manifest)
        return 1
    manifest.pop("failures", None)
    _atomic_json(manifest_path, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
