"""Audit local JIN extractions of the downloaded rejected GenieCommande PDFs.

This is an extraction-only audit: it never creates annotations or training data.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path

import fitz
import httpx


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _value(field):
    return field.get("value") if isinstance(field, dict) else field


def _pdf_text(path: Path) -> list[str]:
    with fitz.open(path) as pdf:
        return [page.get_text() for page in pdf]


def _get_extraction(path: Path, output: Path, api: str, client: httpx.Client):
    if output.exists():
        return _read_json(output)
    with path.open("rb") as stream:
        response = client.post(api, files={"file": (path.name, stream, "application/pdf")})
    response.raise_for_status()
    payload = response.json()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8080/api/extract")
    args = parser.parse_args()
    root = args.root.resolve()
    sample = _read_json(root / "sample_files.json")
    manifest = [json.loads(line) for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    listing = _read_json(root / "listing_snapshot.json")
    by_id = {item["order_id"]: item for item in listing}
    by_hash = collections.defaultdict(list)
    for item in manifest:
        by_hash[item["sha256"]].append(item)
    extraction_dir = root / "review_campaign" / "extractions"
    records = []
    errors = []
    with httpx.Client(timeout=600) as client:
        for index, name in enumerate(sample, 1):
            path = root / "pdf" / name
            digest = _digest(path)
            out = extraction_dir / f"{index:03d}-{digest[:12]}.json"
            try:
                payload = _get_extraction(path, out, args.api, client)
                result = payload.get("normalized_output") or {}
                doc = result.get("document") or {}
                order = result.get("order") or {}
                quality = result.get("quality") or {}
                address_roles = order.get("addresses") or {}
                selected_addresses = {
                    role: ((candidate.get("selected") or {}).get("value"))
                    for role, candidate in address_roles.items()
                    if isinstance(candidate, dict)
                } if isinstance(address_roles, dict) else {}
                lines = order.get("line_items") or []
                pages = _pdf_text(path)
                text = "\n".join(pages)
                text_comparison_available = len(text.strip()) >= 100
                compact_text = "".join(ch for ch in text.upper() if ch.isalnum())
                refs = [str((line.get("references") or {}).get("article_number") or "") for line in lines]
                line_pages = collections.Counter(str((line.get("evidence") or {}).get("page")) for line in lines)
                unsupported_refs = [
                    ref for ref in refs
                    if text_comparison_available and ref
                    and "".join(ch for ch in ref.upper() if ch.isalnum()) not in compact_text
                ]
                net = [(line.get("amounts") or {}).get("net") for line in lines]
                numeric_net = [float(value) for value in net if isinstance(value, (float, int))]
                total = order.get("totals") or {}
                target = next((total.get(key) for key in ("total_net", "subtotal", "total_before_tax") if isinstance(total.get(key), (float, int))), None)
                matches = by_hash.get(digest, [])
                issues = [by_id.get(item["order_id"], {}).get("issue") for item in matches]
                record = {
                    "index": index,
                    "pdf": name,
                    "sha256": digest,
                    "source_file_names": sorted({item["file_name"] for item in matches}),
                    "production_order_ids": [item["order_id"] for item in matches],
                    "production_rejection_reasons": issues,
                    "pdf_pages": len(pages),
                    "pdf_text_characters": len(text.strip()),
                    "reference_text_comparison_available": text_comparison_available,
                    "document_type": doc.get("type"),
                    "order_number": _value(order.get("customer_order_number")),
                    "order_number_evidence": (order.get("customer_order_number") or {}).get("evidence") if isinstance(order.get("customer_order_number"), dict) else None,
                    "requested_delivery_date": _value(order.get("requested_delivery_date")),
                    "buyer_name": (order.get("parties") or {}).get("buyer", {}).get("name"),
                    "supplier_name": (order.get("parties") or {}).get("supplier", {}).get("name"),
                    "selected_addresses": selected_addresses,
                    "delivery_address": selected_addresses.get("ship_to"),
                    "line_count": len(lines),
                    "line_pages": dict(line_pages),
                    "article_references": refs,
                    "unsupported_article_references": unsupported_refs,
                    "line_net_sum": round(sum(numeric_net), 2),
                    "line_net_count": len(numeric_net),
                    "extracted_total_net": target,
                    "line_sum_check": ((quality.get("business_validation") or {}).get("checks") or {}).get("line_sum"),
                    "quality_decision": quality.get("decision"),
                    "review_reasons": [reason.get("code") for reason in quality.get("review_reasons") or []],
                    "extraction_file": str(out.relative_to(root)),
                }
                records.append(record)
                print(f"[{index}/{len(sample)}] {name}: {len(lines)} lines, {quality.get('decision')}", flush=True)
            except Exception as exc:
                errors.append({"index": index, "pdf": name, "error": f"{type(exc).__name__}: {exc}"})
                print(f"[{index}/{len(sample)}] ERROR {name}: {type(exc).__name__}: {exc}", flush=True)
    report = {
        "scope": "extraction_only_no_annotation_or_training",
        "requested_unique_pdfs": len(sample),
        "analyzed_unique_pdfs": len(records),
        "production_rejected_dossiers": len(manifest),
        "errors": errors,
        "document_types": dict(collections.Counter(record["document_type"] for record in records)),
        "quality_decisions": dict(collections.Counter(record["quality_decision"] for record in records)),
        "zero_line_count": sum(record["line_count"] == 0 for record in records),
        "unsupported_reference_documents": sum(bool(record["unsupported_article_references"]) for record in records),
        "records": records,
    }
    output = root / "jin_extraction_audit.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {output}", flush=True)


if __name__ == "__main__":
    main()
