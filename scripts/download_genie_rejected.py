"""Read-only, resumable download of GenieCommande orders with status REJECTED.

The API token is read from one stdin line, never accepted on the command line
or written to a report. Output belongs in an ignored, access-controlled folder.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PAGE_SIZE = 100
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
RETRY_CODES = {429, 500, 502, 503, 504}


def _open(base_url: str, path: str, token: str):
    request = Request(
        base_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, application/pdf"},
        method="GET",
    )
    for attempt in range(4):
        try:
            return urlopen(request, timeout=60)
        except HTTPError as exc:
            if exc.code not in RETRY_CODES or attempt == 3:
                raise
        except URLError:
            if attempt == 3:
                raise
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("Unreachable retry state")


def list_rejected(base_url: str, token: str) -> tuple[list[dict], dict[str, int], int]:
    """Page through every order; the API's status query groups statuses."""
    selected: dict[str, dict] = {}
    status_counts: Counter[str] = Counter()
    page = 1
    scanned = 0
    while True:
        query = urlencode({"status": "all", "sort": "createdAt", "direction": "asc",
                           "page": page, "page_size": PAGE_SIZE})
        with _open(base_url, f"/api/orders/page?{query}", token) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("Order page exceeds the configured JSON size limit")
        payload = json.loads(raw)
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise ValueError("Unexpected order listing response")
        for row in rows:
            if not isinstance(row, dict):
                continue
            scanned += 1
            status = str(row.get("statusCode") or "").upper()
            status_counts[status or "UNKNOWN"] += 1
            order_id = str(row.get("orderId") or "").strip()
            if status == "REJECTED" and order_id:
                selected[order_id] = {"order_id": order_id, "status_code": status,
                                      "file_name": str(row.get("fileName") or ""),
                                      "issue": str(row.get("issue") or ""),
                                      "created_at": str(row.get("createdAt") or "")}
        total_pages = int(payload.get("totalPages") or 0)
        print(f"Page {page}/{max(page, total_pages)}: {len(selected)} dossiers rejetés repérés", flush=True)
        if not rows or page >= total_pages:
            break
        page += 1
    return list(selected.values()), dict(status_counts), scanned


def _existing_downloads(manifest_path: Path, pdf_dir: Path) -> dict[str, dict]:
    if not manifest_path.is_file():
        return {}
    existing: dict[str, dict] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        filename = str(entry.get("local_pdf") or "")
        if (filename and Path(filename).name == filename
                and (pdf_dir / filename).is_file()
                and hashlib.sha256((pdf_dir / filename).read_bytes()).hexdigest() == entry.get("sha256")):
            existing[str(entry["order_id"])] = entry
    return existing


def _download_pdf(base_url: str, token: str, order_id: str, pdf_dir: Path) -> dict:
    safe_stem = hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:24]
    target = pdf_dir / f"{safe_stem}.pdf"
    temporary = pdf_dir / f"{safe_stem}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with _open(base_url, f"/api/orders/{quote(order_id, safe='')}/pdf", token) as response:
            if response.headers.get_content_type() != "application/pdf":
                raise ValueError("Order endpoint did not return a PDF")
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise ValueError("PDF exceeds the configured size limit")
                    output.write(chunk)
                    digest.update(chunk)
        with temporary.open("rb") as source:
            if b"%PDF-" not in source.read(1024):
                raise ValueError("Downloaded content has no PDF signature")
        os.replace(temporary, target)
        return {"local_pdf": target.name, "sha256": digest.hexdigest(), "bytes": size}
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.base_url.lower().startswith("https://"):
        parser.error("Only HTTPS production endpoints are supported")
    if not args.dry_run and args.output_dir is None:
        parser.error("--output-dir is required for downloads")

    if sys.stdin.isatty():
        token = getpass("Jeton API (masqué) : ").strip()
    else:
        token = sys.stdin.readline().strip()
    if not token:
        raise SystemExit("No API token supplied on stdin")
    try:
        selected, status_counts, scanned = list_rejected(args.base_url, token)
        if args.dry_run:
            print(json.dumps({"scanned": scanned, "unique_rejected": len(selected),
                              "status_counts": status_counts}, ensure_ascii=False, indent=2))
            return 0

        output_dir = args.output_dir.resolve()
        pdf_dir = output_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "listing_snapshot.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_path = output_dir / "manifest.jsonl"
        selected_ids = {order["order_id"] for order in selected}
        existing = {order_id: entry for order_id, entry in
                    _existing_downloads(manifest_path, pdf_dir).items() if order_id in selected_ids}
        failures: list[dict] = []
        with manifest_path.open("a", encoding="utf-8") as manifest:
            for index, order in enumerate(selected, 1):
                order_id = order["order_id"]
                if order_id in existing:
                    continue
                try:
                    downloaded = _download_pdf(args.base_url, token, order_id, pdf_dir)
                    entry = {**order, **downloaded,
                             "downloaded_at": datetime.now(timezone.utc).isoformat()}
                    manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    manifest.flush()
                    print(f"PDF {index}/{len(selected)} enregistré", flush=True)
                except (HTTPError, URLError, OSError, ValueError) as exc:
                    failures.append({"order_id": order_id, "error_type": type(exc).__name__,
                                     "http_status": exc.code if isinstance(exc, HTTPError) else None})
                    print(f"PDF {index}/{len(selected)} en échec ({type(exc).__name__})", flush=True)
        report = {"scanned": scanned, "unique_rejected": len(selected),
                  "already_present": len(existing), "downloaded_now": len(selected) - len(existing) - len(failures),
                  "failures": failures, "status_counts": status_counts,
                  "qualification": "Snapshot of REJECTED status at listing time; PDFs are not validated training labels."}
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "failures"},
                         ensure_ascii=False, indent=2))
        return int(bool(failures))
    except HTTPError as exc:
        raise SystemExit(f"Production API request failed with HTTP {exc.code}") from None
    except URLError as exc:
        raise SystemExit(f"Production API connection failed ({type(exc.reason).__name__})") from None


if __name__ == "__main__":
    raise SystemExit(main())
