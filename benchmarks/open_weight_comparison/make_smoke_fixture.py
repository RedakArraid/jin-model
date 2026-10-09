from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz


def build_fixture(output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "synthetic_purchase_order.pdf"
    truth_path = output_dir / "ground_truth.json"

    document = fitz.open()
    page = document.new_page(width=595, height=842)

    lines = [
        (50, 55, "PURCHASE ORDER"),
        (50, 90, "Order number: PO-SMOKE-0001"),
        (50, 112, "Order date: 2026-10-09"),
        (50, 155, "SUPPLIER"),
        (50, 177, "Example Supplier SAS"),
        (50, 199, "12 RUE DES TESTS"),
        (50, 221, "93700 DRANCY"),
        (330, 155, "SHIP TO"),
        (330, 177, "Example Bosch Site"),
        (330, 199, "32 AVENUE DE LA LIVRAISON"),
        (330, 221, "78300 POISSY"),
        (50, 285, "Reference        Description               Qty   Unit   Unit price   Total"),
        (50, 310, "MAT-000123       Synthetic valve           2     PC     100.00       200.00"),
        (350, 390, "Net: 200.00"),
        (350, 412, "VAT: 40.00"),
        (350, 434, "Gross: 240.00"),
        (350, 456, "Amount due: 240.00"),
    ]
    for x, y, text in lines:
        size = 16 if text == "PURCHASE ORDER" else 10
        page.insert_text((x, y), text, fontsize=size)

    document.set_metadata({
        "title": "Synthetic benchmark purchase order",
        "subject": "Technical smoke fixture only - not Bosch business ground truth",
    })
    document.save(pdf_path)
    document.close()

    truth = {
        "schema_version": "jin-open-weight-ground-truth-v1",
        "qualification": (
            "Synthetic technical smoke fixture. Never publish these metrics "
            "as Bosch/JIN business benchmark results."
        ),
        "documents": {
            pdf_path.name: {
                "_review": {
                    "status": "validated",
                    "reviewer": "deterministic_fixture_generator",
                    "reviewed_at": "2026-10-09",
                    "synthetic": True,
                    "notes": "Known values inserted programmatically into the PDF.",
                },
                "document_type": "purchase_order",
                "order_number": "PO-SMOKE-0001",
                "order_date": "2026-10-09",
                "parties": {
                    "supplier": {
                        "name": "Example Supplier SAS",
                        "address": {
                            "house_number": "12",
                            "street_type": "RUE",
                            "street_name": "DES TESTS",
                            "postal_code": "93700",
                            "city": "DRANCY",
                        },
                    },
                    "ship_to": {
                        "name": "Example Bosch Site",
                        "address": {
                            "house_number": "32",
                            "street_type": "AVENUE",
                            "street_name": "DE LA LIVRAISON",
                            "postal_code": "78300",
                            "city": "POISSY",
                        },
                    },
                },
                "lines": [
                    {
                        "line_index": 0,
                        "reference": "MAT-000123",
                        "description": "Synthetic valve",
                        "quantity": 2,
                        "unit": "PC",
                        "unit_price": "100.00",
                        "line_total": "200.00",
                    }
                ],
                "totals": {
                    "net": "200.00",
                    "vat": "40.00",
                    "gross": "240.00",
                    "amount_due": "240.00",
                },
            }
        },
    }
    truth_path.write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "pdf": str(pdf_path),
        "ground_truth": str(truth_path),
        "qualification": truth["qualification"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic synthetic purchase-order smoke fixture."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/open_weight_smoke_fixture"),
    )
    args = parser.parse_args(argv)
    result = build_fixture(args.output_dir.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
