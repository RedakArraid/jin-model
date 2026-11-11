"""Re-extract the local rejected-PDF sample and compare source-reviewed checks.

The PDF corpus and results live under ignored data/; this script contains no
production credentials and does not create human-validated training labels.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from pathlib import Path

import httpx


EXPECTED = {
    2: {"supplier_name": "Bosch pièces de rechange"},
    5: {"min_lines": 8, "required_refs": ["7733701939", "7733703609", "7733701938"],
        "order_date": "2026-11-09", "total_before_tax": 7420.26, "document_sum": 7420.26},
    6: {"min_lines": 4, "required_refs": ["7733703483", "7733701937", "7733701806", "7733701810"]},
    8: {"supplier_name": "Bosch pièces de rechange"},
    9: {"min_lines": 8, "required_refs": ["7733702278", "7736200422"],
        "buyer_name": "CLIMANET SERVICES", "buyer_email_domain": "climanet.com"},
    11: {"min_lines": 4, "required_refs": ["8738213465"]},
    18: {"supplier_name": "Bosch pièces de rechange"},
    14: {"min_lines": 8, "buyer_name_excludes": ["DATE"],
         "ship_to_excludes": ["LIVRAISON UNIQUEMENT", "POINT DE TERMINAISON", "ENTRE 7H ET 11H"]},
    19: {"min_lines": 6, "max_price": 2000, "supplier_name": "LEBLANC"},
    20: {"min_lines": 1, "required_refs": ["87168395930"]},
    21: {"min_lines": 10, "order_number": "NC 9828", "document_sum": 8713.71},
    24: {"min_lines": 2, "required_refs": ["7716780401", "7716780263"],
         "order_number": "7864", "order_date": "2026-02-09", "total_before_tax": 141.03},
    25: {"min_lines": 2, "order_number": "00002539", "max_quantity": 2},
    27: {"min_lines": 9, "order_number": "10/92 983", "supplier_name": "E.L.M. LEBLANC BOSCH"},
    33: {"supplier_name": "Bosch pièces de rechange"},
}


def field_value(value):
    return value.get("value") if isinstance(value, dict) else value


def extract(index: int, path: Path, api: str, output: Path, baseline_lines: int):
    for attempt in range(3):
        try:
            with httpx.Client(timeout=600, trust_env=False) as client, path.open("rb") as stream:
                response = client.post(api, files={"file": (path.name, stream, "application/pdf")})
                response.raise_for_status()
                payload = response.json()
            break
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    normalized = payload.get("normalized_output") or {}
    order = normalized.get("order") or {}
    lines = order.get("line_items") or []
    refs = [str((line.get("references") or {}).get("article_number") or "") for line in lines]
    prices = [float((line.get("pricing") or {}).get("unit_price") or 0) for line in lines]
    quantities = [float(line.get("quantity") or 0) for line in lines]
    order_number = str(field_value(order.get("customer_order_number")) or "")
    order_date = str(field_value(order.get("order_date")) or "")
    total_before_tax = (order.get("totals") or {}).get("total_before_tax")
    line_sum = (((normalized.get("quality") or {}).get("business_validation") or {}).get("checks") or {}).get("line_sum") or {}
    document_sum = line_sum.get("document_sum")
    supplier_name = str((((payload.get("business_extractions") or {}).get("purchase_order") or {}).get("supplier") or {}).get("name") or "")
    buyer_name = ((order.get("parties") or {}).get("buyer") or {}).get("name")
    buyer_email = (((order.get("parties") or {}).get("buyer") or {}).get("contact") or {}).get("email")
    ship_to_address = str((((order.get("addresses") or {}).get("ship_to") or {}).get("selected") or {}).get("value") or "")
    expected = EXPECTED.get(index, {})
    checks = {
        "line_count_not_reduced": len(lines) >= baseline_lines,
        "line_count": len(lines) >= expected.get("min_lines", 0),
        "required_refs": all(ref in refs for ref in expected.get("required_refs", [])),
        "max_price": max(prices, default=0) <= expected.get("max_price", float("inf")),
        "max_quantity": max(quantities, default=0) <= expected.get("max_quantity", float("inf")),
        "order_number": order_number == expected["order_number"] if "order_number" in expected else True,
        "order_date": order_date == expected["order_date"] if "order_date" in expected else True,
        "total_before_tax": abs(float(total_before_tax or 0) - expected["total_before_tax"]) <= 0.02
        if "total_before_tax" in expected else True,
        "document_sum": abs(float(document_sum or 0) - expected["document_sum"]) <= 0.02
        if "document_sum" in expected else True,
        "supplier_name": supplier_name == expected["supplier_name"] if "supplier_name" in expected else True,
        "buyer_name": buyer_name == expected["buyer_name"] if "buyer_name" in expected else True,
        "buyer_email_domain": str(buyer_email or "").endswith("@" + expected["buyer_email_domain"])
        if "buyer_email_domain" in expected else True,
        "buyer_name_excludes": all(term not in str(buyer_name or "").upper()
                                   for term in expected.get("buyer_name_excludes", [])),
        "ship_to_excludes": all(term not in ship_to_address.upper() for term in expected.get("ship_to_excludes", [])),
    }
    return {
        "index": index, "pdf": path.name, "line_count": len(lines),
        "refs": refs, "prices": prices, "quantities": quantities,
        "order_number": order_number, "order_date": order_date,
        "total_before_tax": total_before_tax, "document_sum": document_sum,
        "supplier_name": supplier_name,
        "buyer_name": buyer_name, "buyer_email": buyer_email, "ship_to_address": ship_to_address,
        "checks": checks,
        "pass": all(checks.values()), "output": str(output),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8081/extract")
    parser.add_argument("--all", action="store_true", help="Run all PDFs; unreviewed cases get baseline smoke checks")
    parser.add_argument("--indexes", help="Comma-separated 1-based PDF indexes to check")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    names = json.loads((root / "sample_files.json").read_text(encoding="utf-8"))
    audit = json.loads((root / "jin_extraction_audit.json").read_text(encoding="utf-8"))
    baseline_lines = {int(row["index"]): int(row.get("line_count") or 0)
                      for row in audit.get("records", [])}
    indexes = ([int(value) for value in args.indexes.split(",")]
               if args.indexes else (list(range(1, len(names) + 1)) if args.all else list(EXPECTED)))
    output_dir = root / "regression_after_fixes"
    output_dir.mkdir(exist_ok=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(extract, index, root / "pdf" / names[index - 1], args.api,
                        output_dir / f"{index:03d}.json", baseline_lines.get(index, 0)): index
            for index in indexes
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"index": index, "pass": False, "error": str(exc)}
            print(json.dumps(result, ensure_ascii=False), flush=True)
            results.append(result)
    results.sort(key=lambda item: item["index"])
    (output_dir / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    failed = sum(not item["pass"] for item in results)
    print(f"RESULT {len(results) - failed}/{len(results)} regression/smoke checks passed"
          f" ({len(set(indexes) & set(EXPECTED))} source-reviewed cases)")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
