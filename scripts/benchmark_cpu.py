#!/usr/bin/env python3
"""Reproducible local PDF coverage benchmark, never a substitute for ground truth.

Example: python scripts/benchmark_cpu.py ARCHIVES_CDES_ESKER_PDF_002001-005000 \
    --limit 20 --seed 42 --output benchmarks/cpu-local.json

--file-list accepts a JSON list of paths relative to the input directory. It
preserves that order for replay; otherwise unique PDFs are shuffled with seed 42.
--expected accepts {"file.pdf": {"document_type": "purchase_order", ...}}.
Only supplied fields are compared, as regression checks, not holdout accuracy.
Address checks use address_components: {ship_to: {postal_code: "01704", ...}};
contacts use contacts: {buyer: {name: "...", email: "...", phone: "..."}}.
Roles must match exactly. Text comparisons ignore case and repeated whitespace;
phone/fax comparisons also ignore presentation separators, not country prefixes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any, Callable
from unittest.mock import patch


EXPECTED_FIELDS = {"document_type", "order_number", "line_count", "total_net", "address_components", "contacts"}
ADDRESS_COMPONENT_FIELDS = {
    "building", "building_number", "residence", "entrance", "floor", "unit",
    "house_number", "house_number_suffix", "street_type", "street_name", "street",
    "lieu_dit", "industrial_zone", "business_park", "address_complement", "po_box",
    "tsa", "cs", "postal_routing_code", "postal_code", "city", "cedex",
    "cedex_number", "district", "insee_code", "state", "region", "country", "country_code",
}
CONTACT_FIELDS = {"name", "firstname", "lastname", "department", "email", "phone", "fax"}
REFERENCE_FIELDS = (
    "material_number", "supplier_material_number", "manufacturer_part_number",
    "customer_material_number", "sku", "ean", "gtin",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_documents(
    folder: Path, limit: int = 20, seed: int = 42,
    file_list: list[str] | None = None,
) -> tuple[list[tuple[Path, str]], dict[str, Any]]:
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError("Input must be an existing directory")
    if limit < 1:
        raise ValueError("--limit must be positive")
    if file_list is not None:
        if not isinstance(file_list, list) or not all(isinstance(name, str) for name in file_list):
            raise ValueError("--file-list must contain a JSON list of relative PDF paths")
        candidates = []
        for name in file_list:
            path = (folder / name).resolve()
            if Path(name).is_absolute() or not path.is_relative_to(folder):
                raise ValueError("Replay paths must remain within the input directory")
            if path.suffix.lower() != ".pdf" or not path.is_file():
                raise ValueError(f"Replay PDF is missing or not a PDF: {name}")
            candidates.append(path)
    else:
        candidates = sorted(
            (path for path in folder.rglob("*") if path.is_file()
             and path.suffix.lower() == ".pdf" and path.resolve().is_relative_to(folder)),
            key=lambda path: path.relative_to(folder).as_posix(),
        )

    unique = {}
    for path in candidates:
        unique.setdefault(sha256_file(path), path)
    documents = [(path, digest) for digest, path in unique.items()]
    if file_list is None:
        random.Random(seed).shuffle(documents)
    selected = documents[:limit]
    return selected, {
        "method": "explicit_file_list" if file_list is not None else "seeded_unique_sha256_sample",
        "seed": seed, "limit": limit, "candidate_files": len(candidates),
        "unique_sha256": len(documents), "duplicate_files": len(candidates) - len(documents),
        "selected_files": [path.relative_to(folder).as_posix() for path, _ in selected],
    }


@contextmanager
def network_disabled():
    """Guard this single-process benchmark against accidental Python networking."""
    def deny(*args, **kwargs):
        raise RuntimeError("NETWORK_DISABLED_FOR_LOCAL_BENCHMARK")

    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.socket.sendto"):
            stack.enter_context(patch(target, deny))
        yield


def create_local_extractor(models_dir: Path | None = None):
    """Import heavy dependencies only when executing the CLI, not during tests."""
    root = Path(__file__).resolve().parents[1]
    core_root = next((path for path in (root / "model", root, Path("/app"))
                      if (path / "uda" / "engine.py").is_file()), None)
    if core_root is None:
        raise RuntimeError("Packaged core engine was not found in model/ or /app")
    for path in (core_root, root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    os.environ.update({
        "JIN_OFFLINE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "CUDA_VISIBLE_DEVICES": "",
    })

    from uda.engine import UniversalDocumentAI
    from jin_runtime import __version__
    from jin_runtime.ban_reference import LocalBanReference
    from jin_runtime.customer_agency_codes import enrich_customer_agency_codes
    from jin_runtime.delivery_addresses import enrich_delivery_addresses
    from jin_runtime.document_router import CorpusDocumentRouter
    from jin_runtime.extraction_gate import apply_extraction_gate
    from jin_runtime.generic_document_fields import enrich_generic_document_fields
    from jin_runtime.grouped_order_fields import enrich_grouped_order_fields
    from jin_runtime.learning import StatisticalAddressLearner
    from jin_runtime.offline import configure_core_offline
    from jin_runtime.output_quality import audit_and_repair_output
    from jin_runtime.pdf_cpu_router import CpuPdfRouter
    from jin_runtime.weak_field_reconciliation import reconcile_weak_fields
    from jin_runtime.weak_field_router import WeakFieldRouter

    models_dir = (models_dir or root / "data" / "learning").resolve()
    engine = UniversalDocumentAI()
    configure_core_offline(engine)
    for owner in (engine, engine.po_engine):
        owner.config.setdefault("azure_document_intelligence", {})["enabled"] = False
        owner.config.setdefault("feedback", {})["enabled"] = False
    learner = StatisticalAddressLearner(model_dir=models_dir)
    ban_reference = LocalBanReference(root / "data" / "reference" / "ban")
    routers = {
        "document_router": CorpusDocumentRouter(models_dir / "corpus_text_router.joblib"),
        "pdf_router": CpuPdfRouter(models_dir / "jin-pdf-fusion-router-v3-cpu.joblib"),
        "field_router": WeakFieldRouter(models_dir / "jin-field-weak-router-v2-cpu.joblib"),
    }

    def extract(path: Path) -> dict[str, Any]:
        payload = learner.enrich(engine.extract(path).model_dump(mode="json", exclude_none=True))
        route = routers["document_router"].predict_payload(payload)
        if route:
            payload["document_statistical_router"] = route
        data = path.read_bytes()
        for name, key in (("pdf_router", "document_pdf_router"),
                          ("field_router", "weak_field_suggestions")):
            router = routers[name]
            if router.loaded:
                try:
                    payload[key] = router.predict_bytes(data)
                except Exception as exc:
                    payload.setdefault("runtime_warnings", []).append(
                        {"component": name, "detail": type(exc).__name__}
                    )
            else:
                payload.setdefault("runtime_warnings", []).append(
                    {"component": name, "detail": "MODEL_NOT_LOADED"}
                )
        payload = reconcile_weak_fields(payload)
        payload = audit_and_repair_output(payload, repair=True)
        payload = ban_reference.enrich(payload)
        payload = enrich_customer_agency_codes(payload)
        payload = enrich_delivery_addresses(payload)
        payload = enrich_generic_document_fields(payload)
        payload = enrich_grouped_order_fields(payload)
        return apply_extraction_gate(payload)

    metadata = {
        "core_version": engine.version, "runtime_version": __version__,
        "offline": True, "device": "cpu", "geocoding_enabled": False,
        "external_models_enabled": False, "feedback_training": False,
        "local_ban_reference": ban_reference.status(),
        "models_loaded": {name: bool(router.status().get("loaded")) for name, router in routers.items()},
        "source_sha256": {
            "core/" + name: sha256_file(core_root / name)
            for name in ("uda/business.py", "uda/semantic.py", "po_ocr/extract.py",
                         "po_ocr/pipeline.py", "po_ocr/pdf_native.py",
                         "po_ocr/address_roles.py", "po_ocr/address_intelligence.py")
        } | {
            "runtime/" + name: sha256_file(root / "jin_runtime" / name)
            for name in ("extraction_gate.py", "order_numbers.py", "output_quality.py",
                         "weak_field_router.py", "geometry_fields.py", "ban_reference.py",
                         "clean_output.py", "delivery_addresses.py",
                         "customer_agency_codes.py", "weak_field_reconciliation.py",
                         "generic_document_fields.py", "grouped_order_fields.py")
        },
    }
    return extract, metadata


def _value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else value


def _present(value: Any) -> bool:
    return _value(value) is not None and _value(value) != ""


def _parts(payload: dict[str, Any]):
    po = (payload.get("business_extractions") or {}).get("purchase_order") or {}
    if not po and payload.get("purchase_order"):
        po = payload
    return po, po.get("purchase_order") or po, po.get("lines") or []


def summarize_document(payload: dict[str, Any]) -> dict[str, Any]:
    """Only counts, roles and diagnostic codes; no address/contact text is kept."""
    po, header, lines = _parts(payload)
    document = payload.get("document") or {}
    weak = payload.get("weak_field_suggestions") or {}
    decision = payload.get("extraction_decision") or {}
    addresses = po.get("business_addresses") or payload.get("business_addresses") or []
    reasons = {str(item.get("code")) for item in decision.get("reasons") or []}
    issues = (payload.get("output_quality") or {}).get("issues") or []
    quality = payload.get("quality") or {}
    roles = Counter(str(item.get("role") or "unknown") for item in addresses)
    return {
        "document_type": document.get("primary_document_type")
        or document.get("detected_document_type") or document.get("document_type"),
        "pages": document.get("page_count"), "weak_pages_processed": weak.get("pages_processed"),
        "text_source": weak.get("text_source"),
        "ocr_required": bool((po.get("document") or {}).get("ocr_required")
                             or any(item.get("text_source") == "tesseract_ocr"
                                    for item in weak.get("page_text_sources") or [])),
        "order_number_present": _present(header.get("number") or header.get("order_number")),
        "order_date_present": _present(header.get("order_date")),
        "total_net_present": _present((po.get("totals") or {}).get("total_net")),
        "line_count": len(lines),
        "lines_missing_reference": sum(not any(_present(line.get(key)) for key in REFERENCE_FIELDS) for line in lines),
        "lines_missing_quantity": sum(not _present(line.get("quantity")) for line in lines),
        "lines_missing_unit_price": sum(not any(_present(line.get(key)) for key in ("net_unit_price", "unit_price")) for line in lines),
        "address_count": len(addresses), "address_roles": dict(sorted(roles.items())),
        "addresses_missing_locality": sum(not ((item.get("address") or {}).get("postal_code")
                                               and (item.get("address") or {}).get("city")) for item in addresses),
        "party_name_presence": {role: bool((po.get(role) or {}).get("name")) for role in ("buyer", "supplier")},
        "party_contact_presence": {
            role: any(_present((po.get(role) or {}).get(key)) for key in ("email", "phone"))
            or any(_present(value) for value in ((po.get(role) or {}).get("contact") or {}).values())
            for role in ("buyer", "supplier")
        },
        "requires_review": bool(decision.get("requires_review", quality.get("requires_human_review", True))),
        "decision": decision.get("status", "NOT_EVALUATED"), "review_reasons": sorted(reasons),
        "output_issue_codes": sorted({str(item.get("code")) for item in issues}),
        "runtime_warning_components": sorted({str(item.get("component")) for item in payload.get("runtime_warnings") or []}),
    }


def validate_expected(expected: dict[str, Any]) -> None:
    if not isinstance(expected, dict) or set(expected) - EXPECTED_FIELDS:
        raise ValueError("Unsupported expected field in regression data")
    for group, allowed in (("address_components", ADDRESS_COMPONENT_FIELDS), ("contacts", CONTACT_FIELDS)):
        if group not in expected:
            continue
        by_role = expected[group]
        if not isinstance(by_role, dict) or not by_role:
            raise ValueError(f"{group} must map roles to expected component values")
        for role, components in by_role.items():
            if not isinstance(role, str) or not role or not isinstance(components, dict) or not components:
                raise ValueError(f"{group} must map roles to expected component values")
            if set(components) - allowed:
                raise ValueError(f"Unsupported component in {group}")
            if any(isinstance(value, (dict, list)) for value in components.values()):
                raise ValueError(f"{group} component values must be scalar")


def _comparable_component(value: Any, field: str) -> Any:
    value = _value(value)
    if not isinstance(value, str):
        return value
    value = " ".join(value.split()).casefold()
    if field in {"phone", "fax"}:
        value = value.translate(str.maketrans("", "", " .()-"))
    return value


def _component_check(
    path: str, field: str, wanted: Any, values: list[Any], role_present: bool,
) -> dict[str, Any]:
    unique_values = []
    normalized_values = []
    for value in values or [None]:
        normalized = _comparable_component(value, field)
        if normalized not in normalized_values:
            normalized_values.append(normalized)
            unique_values.append(_value(value))
    ambiguous = len(unique_values) > 1
    match = role_present and not ambiguous and normalized_values[0] == _comparable_component(wanted, field)
    check = {
        "field": path, "expected": wanted, "actual": unique_values[0] if not ambiguous else None,
        "match": match, "ambiguous": ambiguous,
        "comparison": "casefold_whitespace_phone_separators" if field in {"phone", "fax"} else "casefold_whitespace",
    }
    if ambiguous:
        check["candidate_values"] = unique_values
    if not match:
        check["reason"] = "ROLE_MISSING" if not role_present else "CONFLICTING_VALUES" if ambiguous else "VALUE_MISMATCH"
    return check


def compare_expected(payload: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    validate_expected(expected)
    po, header, lines = _parts(payload)
    addresses = po.get("business_addresses") or payload.get("business_addresses") or []
    actual = {
        "document_type": summarize_document(payload)["document_type"],
        "order_number": _value(header.get("number") or header.get("order_number")),
        "line_count": len(lines), "total_net": _value((po.get("totals") or {}).get("total_net")),
    }
    checks = []
    for field, wanted in expected.items():
        if field in {"address_components", "contacts"}:
            for role, components in wanted.items():
                # No role aliasing and no shared_with_roles expansion: otherwise
                # a correct location assigned to the wrong role would pass.
                records = [item for item in addresses if item.get("role") == role]
                party = po.get(role) or {}
                for component, target in components.items():
                    if field == "address_components":
                        values = [(record.get("address") or {}).get(component) for record in records]
                        present = bool(records)
                    else:
                        contact = party.get("contact") or {}
                        values = [contact.get(component)] if _present(contact.get(component)) else []
                        if component in {"email", "phone", "fax"} and _present(party.get(component)):
                            values.append(party[component])
                        for record in records:
                            key = f"contact_{component}"
                            if component in {"name", "email", "phone"} and _present(record.get(key)):
                                values.append(record[key])
                        present = bool(records) or role in po
                    checks.append(_component_check(
                        f"{field}.{role}.{component}", component, target, values, present,
                    ))
            continue
        found = actual[field]
        if field == "total_net" and wanted is not None and found is not None:
            try:
                match = Decimal(str(wanted).replace(" ", "").replace(",", ".")) == Decimal(str(found).replace(" ", "").replace(",", "."))
            except InvalidOperation:
                match = False
        elif field == "document_type" and isinstance(wanted, str) and isinstance(found, str):
            match = wanted.casefold() == found.casefold()
        elif field == "order_number" and isinstance(wanted, str) and isinstance(found, str):
            from jin_runtime.order_numbers import normalize_identifier
            match = normalize_identifier(wanted) == normalize_identifier(found)
        else:
            match = wanted == found
        check = {"field": field, "expected": wanted, "actual": found, "match": match}
        if field == "order_number":
            check["comparison"] = "case_and_whitespace_only_preserve_zeroes_and_punctuation"
        checks.append(check)
    return checks


def run_benchmark(
    folder: Path, extract: Callable[[Path], dict[str, Any]], *, limit: int = 20,
    seed: int = 42, file_list: list[str] | None = None,
    expected: dict[str, dict[str, Any]] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    folder = folder.resolve()
    expected = expected or {}
    if not isinstance(expected, dict):
        raise ValueError("--expected must be a JSON object keyed by relative filename")
    for fields in expected.values():
        validate_expected(fields)
    selected, selection = select_documents(folder, limit, seed, file_list)
    results = []
    for path, digest in selected:
        name = path.relative_to(folder).as_posix()
        item: dict[str, Any] = {"filename": name, "sha256": digest}
        started = time.perf_counter()
        try:
            payload = extract(path)
            item.update(summarize_document(payload))
            if name in expected:
                item["regression_checks"] = compare_expected(payload, expected[name])
        except Exception as exc:
            # Exception messages can contain OCR text or full private paths.
            item.update(error={"type": type(exc).__name__}, requires_review=True)
        item["duration_seconds"] = round(time.perf_counter() - started, 4)
        results.append(item)
        if progress is not None:
            progress(item)

    successful = [item for item in results if "error" not in item]
    checks = [check for item in results for check in item.get("regression_checks", [])]
    durations = [item["duration_seconds"] for item in results]
    summary = {
        "processed": len(results), "succeeded": len(successful), "errors": len(results) - len(successful),
        "requires_review": sum(item["requires_review"] for item in results),
        "document_types": dict(Counter(item["document_type"] or "unknown" for item in successful)),
        "presence_counts": {key: sum(item[key] for item in successful) for key in (
            "order_number_present", "order_date_present", "total_net_present")},
        "line_count": sum(item["line_count"] for item in successful),
        "missing_line_fields": {key: sum(item[key] for item in successful) for key in (
            "lines_missing_reference", "lines_missing_quantity", "lines_missing_unit_price")},
        "review_reason_counts": dict(Counter(code for item in successful for code in item["review_reasons"])),
        "duration_seconds_total": round(sum(durations), 4),
        "duration_seconds_median": round(statistics.median(durations), 4) if durations else None,
        "regression": {
            "compared_fields": len(checks), "matched_fields": sum(check["match"] for check in checks),
            "mismatched_fields": sum(not check["match"] for check in checks),
            "expected_files_not_evaluated": sorted(set(expected) - {item["filename"] for item in successful}),
            "qualification": "Explicit regression expectations only; not independent holdout accuracy.",
        },
    }
    return {
        "schema_version": "cpu-coverage-benchmark-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "qualification": "Coverage, completeness and alerts are not factual extraction accuracy. No accuracy is reported without independently reviewed ground truth.",
        "privacy": "By default no extracted addresses, party names, contact details or document text are stored. Optional regression checks expose only explicitly requested scalar fields or address/contact components.",
        "selection": selection, "summary": summary, "documents": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--file-list", type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    file_list = json.loads(args.file_list.read_text(encoding="utf-8-sig")) if args.file_list else None
    expected = json.loads(args.expected.read_text(encoding="utf-8-sig")) if args.expected else None
    initialized_at = time.perf_counter()
    with network_disabled():
        extract, runtime = create_local_extractor(args.models_dir)
        runtime["initialization_seconds"] = round(time.perf_counter() - initialized_at, 4)
        report = run_benchmark(args.folder, extract, limit=args.limit, seed=args.seed,
                               file_list=file_list, expected=expected, progress=lambda item: print(
                                   f"{item['filename']}: {'ERROR' if 'error' in item else item['decision']} "
                                   f"({item['duration_seconds']:.3f}s)", file=sys.stderr, flush=True))
    report["runtime"] = runtime
    # Keep a diagnostic summary even if the host-mounted output becomes
    # unwritable after an expensive OCR run.
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(bool(not report["summary"]["processed"] or report["summary"]["errors"]
                    or report["summary"]["regression"]["mismatched_fields"]))


if __name__ == "__main__":
    raise SystemExit(main())
