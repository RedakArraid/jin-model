from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid
from typing import Any

from po_ocr.config import load_config
from po_ocr.ingest import describe_file
from po_ocr.pipeline import PurchaseOrderOCR

from .business import extract_purchase_order, enhance_purchase_order_with_native_structure
from .models import QualityReport, UniversalDocumentMeta, UniversalDocumentResult, UniversalTable, SourceRef, KeyValue
from .readers import read_any
from .schema import extract_schema_fields
from .semantic import detect_document_type, detect_languages, extract_entities, extract_key_values, extract_structured_key_values, synthetic_pages, classify_pages, aggregate_page_classifications
from .multimodal import run_multimodal
from .learned import load_router



def _fold_text(value: str | None) -> str:
    import unicodedata, re
    txt=unicodedata.normalize("NFKD", value or "").encode("ascii","ignore").decode().casefold()
    return " ".join(re.sub(r"[^a-z0-9]+"," ",txt).split())


def _promote_transaction_continuations(page_classifications, tables, pages, cfg):
    """Promote weak adjacent pages when strong continuation evidence exists.

    A continuation page often contains only the repeated PO header and line table, so a
    standalone classifier may score it below threshold. We require adjacency plus native
    line-table or repeated-order evidence; legal annex pages without line tables are left alone.
    """
    import re
    if not cfg.get("infer_transaction_continuation_pages", True) or not page_classifications:
        return page_classifications
    po_pages={int(x["page"]) for x in page_classifications if x.get("type")=="purchase_order" and x.get("page") is not None}
    if not po_pages:
        return page_classifications
    page_text={int(getattr(pg,"page",i+1)):str(getattr(pg,"text","") or "") for i,pg in enumerate(pages or [])}
    min_headers=int(cfg.get("continuation_min_line_headers",4))
    strong_conf=float(cfg.get("continuation_promoted_confidence",0.84))
    max_gap=int(cfg.get("continuation_max_page_gap",1))
    line_terms=("lig","ligne","produit","article","qte","quantite","prix unitaire","montant","date de livraison","designation","reference fournisseur")
    changed=True
    while changed:
        changed=False
        for cls in page_classifications:
            if cls.get("type")=="purchase_order" or cls.get("page") is None:
                continue
            page=int(cls["page"])
            if not any(0 < page-p <= max_gap for p in po_pages):
                continue
            table_hits=0
            for t in tables:
                if t.source.page is not None and int(t.source.page)!=page: continue
                headers=t.headers or ((t.data or [[]])[0] if t.data else [])
                folded=[_fold_text(str(h or "")) for h in headers]
                hits=sum(1 for term in line_terms if any(term in h for h in folded))
                table_hits=max(table_hits,hits)
            text=page_text.get(page,"")
            top=text[:1200]
            repeated_header=bool(re.search(r"\b(?:cde|commande|purchase\s+order)\b.{0,80}\b(?:n[°ºo]?|no|number|#)\b.{0,80}\d{5,}", top, flags=re.I|re.S))
            page_marker=bool(re.search(r"\bpage\s+\d+\s+(?:sur|of)\s+\d+\b", top, flags=re.I))
            # Product-only continuation pages can omit the PO title and table
            # heading.  Multiple bracketed material IDs with euro amounts are
            # stronger evidence than the page classifier's weak standalone vote.
            bracketed_materials=len(re.findall(r"\[\d{10,11}\]", text))
            euro_amounts=len(re.findall(r"\b\d[\d .]*,\d{2}\s*€", text))
            material_continuation=(bracketed_materials>=2 and euro_amounts>=2)
            if table_hits >= min_headers or (repeated_header and page_marker) or material_continuation:
                cls["type"]="purchase_order"
                cls["confidence"]=round(max(float(cls.get("confidence",0.0)), strong_conf if table_hits>=min_headers or material_continuation else strong_conf-0.05),4)
                cls.setdefault("signals",[]).append(f"continuation-inferred:table_headers={table_hits},repeated_header={repeated_header},bracketed_materials={bracketed_materials}")
                po_pages.add(page); changed=True
    return page_classifications


class UniversalDocumentAI:
    """Hybrid document-understanding engine for common business document formats.

    It uses native parsers whenever possible, OCR for pixel-only pages, and business
    extractors only after the document has been read losslessly into a generic model.
    """

    def __init__(self, config_path: str | Path | None = None):
        self.config = load_config(config_path)
        self.po_engine = PurchaseOrderOCR(config_path)
        self.version = str(self.config.get("universal", {}).get("version", "1.0.0"))
        learned_cfg=self.config.get("learned_models", {}) or {}
        self.learned_router = load_router(Path(__file__).resolve().parents[1]) if learned_cfg.get("enabled", True) else None

    def extract(
        self,
        path: str | Path,
        *,
        schema: dict[str, Any] | None = None,
        force_document_type: str | None = None,
    ) -> UniversalDocumentResult:
        started = time.perf_counter()
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(str(p))
        fd = describe_file(p)

        accepted = set(self.config.get("universal", {}).get("accepted_extensions", []))
        if accepted and fd.extension not in accepted:
            raise ValueError(f"Unsupported extension: .{fd.extension}")
        max_mb = int(self.config.get("universal", {}).get("max_file_size_mb", 200))
        if fd.file_size > max_mb * 1024 * 1024:
            raise ValueError(f"Input exceeds max_file_size_mb={max_mb}")

        read = read_any(
            p,
            self.po_engine,
            max_cells_per_sheet=int(self.config.get("universal", {}).get("max_cells_per_sheet", 100_000)),
        )
        universal_cfg = self.config.get("universal", {}) or {}
        learned_pred = self.learned_router.predict(read.raw_text, top_k=int((self.config.get("learned_models", {}) or {}).get("family_top_k", 3))) if self.learned_router else {"family": None, "confidence": 0.0, "topk": [], "ood_score": 1.0, "trainable_parameters": 0}
        if universal_cfg.get("page_level_classification", True):
            page_classifications = classify_pages(read.pages, read.page_results, family=read.family)
            min_page_conf = float(universal_cfg.get("page_classification_min_confidence", 0.45))
            for cls in page_classifications:
                if float(cls.get("confidence", 0.0)) < min_page_conf:
                    cls["type"] = "generic_document"
                    cls.setdefault("signals", []).append("below-min-page-confidence")
        else:
            page_classifications = []
        page_classifications = _promote_transaction_continuations(
            page_classifications, read.tables, read.page_results or [], universal_cfg
        )
        routing = aggregate_page_classifications(page_classifications, fallback_text=read.raw_text, family=read.family)
        if not universal_cfg.get("transactional_page_priority", True) and not force_document_type:
            legacy_type = detect_document_type(read.raw_text, read.family)
            routing["detected_document_type"] = legacy_type
            routing["primary_document_type"] = legacy_type
            routing["is_composite"] = False
        if not universal_cfg.get("composite_document_detection", True) and not force_document_type:
            routing["detected_document_type"] = routing["primary_document_type"]
            routing["is_composite"] = False
        if force_document_type:
            doc_type = force_document_type
            primary_type = force_document_type
            is_composite = False
            routing_confidence = 1.0
            components = [{"type": force_document_type, "pages": [c.get("page") for c in page_classifications if c.get("page")], "confidence": 1.0}]
        else:
            doc_type = routing["detected_document_type"]
            primary_type = routing["primary_document_type"]
            is_composite = bool(routing["is_composite"])
            routing_confidence = float(routing["confidence"])
            components = routing["components"]
        page_type_map = {int(x["page"]): str(x["type"]) for x in page_classifications if x.get("page") is not None}
        kvs = extract_key_values(read.blocks, read.tables)
        if read.structured_data:
            existing = {(kv.key, str(kv.value)) for kv in kvs}
            for kv in extract_structured_key_values(read.structured_data):
                if (kv.key, str(kv.value)) not in existing:
                    kvs.append(kv)
                    existing.add((kv.key, str(kv.value)))
        entities = extract_entities(read.blocks, page_types=page_type_map if universal_cfg.get("legal_terms_entity_filter", True) else None)

        source_confidences = [b.confidence for b in read.blocks if b.text.strip()]
        if read.page_results:
            source_confidences.extend(pg.confidence for pg in read.page_results)
        source_conf = sum(source_confidences) / len(source_confidences) if source_confidences else 0.0
        text_coverage = 1.0 if read.raw_text.strip() else 0.0
        table_coverage = min(1.0, len(read.tables) / 3.0) if read.tables else 0.0
        kv_coverage = min(1.0, len(kvs) / 10.0) if kvs else 0.0
        overall = round(0.65 * source_conf + 0.20 * text_coverage + 0.10 * table_coverage + 0.05 * kv_coverage, 4)
        threshold = float(self.config.get("universal", {}).get("manual_review_threshold", 0.80))

        meta = UniversalDocumentMeta(
            id=str(uuid.uuid4()), filename=fd.filename, extension=fd.extension,
            mime_type=fd.mime_type, file_size=fd.file_size, sha256=fd.sha256,
            format_family=read.family, page_count=read.page_count, sheet_count=read.sheet_count,
            slide_count=read.slide_count, detected_document_type=doc_type, primary_document_type=primary_type,
            is_composite=is_composite, page_classifications=page_classifications, document_components=components,
            detected_languages=detect_languages(read.raw_text),
            processing_timestamp=datetime.now(timezone.utc).isoformat(), engine_version=self.version,
            learned_document_family=learned_pred.get("family"),
            learned_family_confidence=float(learned_pred.get("confidence",0.0) or 0.0),
            learned_family_topk=list(learned_pred.get("topk",[]) or []),
            learned_ood_score=float(learned_pred.get("ood_score",1.0) or 0.0),
            learned_trainable_parameters=int(learned_pred.get("trainable_parameters",0) or 0),
        )
        result = UniversalDocumentResult(
            document=meta, raw_text=read.raw_text, blocks=read.blocks, tables=read.tables,
            key_values=kvs, entities=entities, embedded_objects=read.embedded_objects,
            pages=read.pages, structured_data=read.structured_data,
            quality=QualityReport(
                overall_confidence=overall, source_confidence=round(source_conf, 4),
                classification_confidence=round(routing_confidence, 4),
                semantic_confidence=round(routing_confidence, 4),
                text_coverage=text_coverage, table_coverage=table_coverage, key_value_coverage=kv_coverage,
                warnings=read.warnings.copy(), requires_human_review=overall < threshold,
            ),
        )

        # Business-specific interpretation is additive. V4.2 routes by page so
        # appended legal terms/annexes cannot poison the transactional parser.
        if primary_type == "purchase_order":
            pages_all = read.page_results or synthetic_pages(read.blocks, read.tables)
            po_page_numbers = {int(x["page"]) for x in page_classifications if x.get("page") is not None and x.get("type") == "purchase_order"}
            if force_document_type == "purchase_order" and not po_page_numbers:
                po_page_numbers = {int(getattr(pg, "page", 1)) for pg in pages_all}
            if universal_cfg.get("isolate_business_pages", True) and po_page_numbers:
                pages = [pg for pg in pages_all if int(getattr(pg, "page", 1)) in po_page_numbers] or pages_all
                po_tables = [t for t in read.tables if t.source.page is None or int(t.source.page) in po_page_numbers]
            else:
                pages = pages_all
                po_tables = list(read.tables)
            if pages:
                business_cfg=dict(self.po_engine.config)
                business_cfg["_learned_family"] = learned_pred.get("family")
                business_cfg["_learned_family_confidence"] = float(learned_pred.get("confidence",0.0) or 0.0)
                business_cfg["_learned_ood_score"] = float(learned_pred.get("ood_score",1.0) or 0.0)
                # Keep annex/legal-footer pages available as read-only role
                # evidence without feeding them into the PO line parser.
                business_cfg["_document_pages"] = pages_all
                if self.learned_router:
                    business_cfg["_learned_number_shape_profiles"] = self.learned_router.shape_profiles
                po = extract_purchase_order(p, pages, business_cfg)
                po = enhance_purchase_order_with_native_structure(po, po_tables, pages, business_cfg)
                result.business_extractions["purchase_order"] = po.model_dump(mode="json", exclude_none=True)
                # V4.4: promote business addresses/roles to a first-class, display-friendly list.
                result.business_extractions["business_addresses"] = [
                    x.model_dump(mode="json", exclude_none=True) for x in po.business_addresses
                ]
                result.business_extractions["additional_charges"] = [
                    x.model_dump(mode="json", exclude_none=True) for x in po.additional_charges
                ]

                # Promote a reliably reconstructed PO line table into the universal layer.
                # This preserves structure even when the generic table detector missed borders/merged OCR rows.
                if po.lines and not result.tables:
                    headers = ["article", "supplier_reference", "description", "quantity", "uom", "unit_price", "net_unit_price", "line_amount", "delivery_date", "tax_code", "quote_number"]
                    data = [[
                        x.material_number, x.supplier_material_number, x.description, x.quantity, x.uom,
                        x.unit_price, x.net_unit_price, x.line_total, x.requested_delivery_date, x.tax_code, x.quote_number
                    ] for x in po.lines]
                    table_conf = sum(float(x.confidence) for x in po.lines) / max(1, len(po.lines))
                    result.tables.append(UniversalTable(
                        id="po_line_items_recovered", name="Purchase order line items",
                        source=SourceRef(page=po.lines[0].page if po.lines else 1),
                        rows=len(data), columns=len(headers), headers=headers, data=data, confidence=table_conf,
                    ))
                    result.quality.table_coverage = max(result.quality.table_coverage, min(1.0, len(data) / 3.0))

                # Promote high-value business fields as generic key/value pairs too.
                promoted = []
                if po.purchase_order.number.value:
                    promoted.append(("purchase_order.number", po.purchase_order.number.value, po.purchase_order.number.final_confidence, po.purchase_order.number.evidence.page))
                if po.purchase_order.order_date.value:
                    promoted.append(("purchase_order.order_date", po.purchase_order.order_date.value, po.purchase_order.order_date.final_confidence, po.purchase_order.order_date.evidence.page))
                if po.supplier.name:
                    promoted.append(("supplier.name", po.supplier.name, 0.92, 1))
                for addr in po.business_addresses:
                    if addr.formatted_address:
                        promoted.append((f"address.{addr.role}", addr.formatted_address, addr.confidence, addr.evidence.page or 1))
                if po.totals.total_net is not None:
                    promoted.append(("totals.total_net", po.totals.total_net, 0.96, 1))
                if po.totals.total_vat is not None:
                    promoted.append(("totals.total_vat", po.totals.total_vat, 0.96, 1))
                if po.totals.grand_total is not None:
                    promoted.append(("totals.grand_total", po.totals.grand_total, 0.97, 1))
                existing_pairs = {(kv.key, str(kv.value)) for kv in result.key_values}
                for key, value, conf, page_no in promoted:
                    pair = (key, str(value))
                    if pair not in existing_pairs:
                        result.key_values.append(KeyValue(key=key, value=value, raw_value=str(value), normalized_value=value, confidence=float(conf), source=SourceRef(page=page_no), extraction_method="business_spatial"))
                        existing_pairs.add(pair)
                result.quality.key_value_coverage = min(1.0, len(result.key_values) / 10.0) if result.key_values else 0.0

                # Separate source quality from semantic/business confidence. A clean PDF
                # must not appear safe when no usable PO fields were structured.
                present_core = [
                    bool(po.purchase_order.number.value), bool(po.purchase_order.order_date.value),
                    bool(po.buyer.name), bool(po.supplier.name), bool(po.lines),
                    po.totals.total_net is not None or po.totals.grand_total is not None,
                ]
                completeness = sum(present_core) / len(present_core)
                validation_map = {"PASS": 1.0, "WARNING": 0.72, "FAIL": 0.25}
                validation_conf = validation_map.get(po.validation.status, 0.5)
                result.quality.business_extraction_confidence = float(po.overall_confidence)
                result.quality.validation_confidence = validation_conf
                result.quality.completeness_confidence = round(completeness, 4)
                family_conf=float(learned_pred.get("confidence",0.0) or 0.0)
                lm_cfg=self.config.get("learned_models", {}) or {}
                min_boost_conf=float(lm_cfg.get("confidence_boost_min_family_confidence",0.90))
                family_signal=family_conf if family_conf>=min_boost_conf else 0.0
                learned_weight=float(lm_cfg.get("semantic_confidence_weight",0.06)) if self.learned_router and family_signal else 0.0
                base_sem=0.55 * float(po.overall_confidence) + 0.45 * routing_confidence
                result.quality.semantic_confidence = round((1.0-learned_weight)*base_sem + learned_weight*family_signal, 4)
                # Learned familiarity is only a small, confidence-gated prior. Financial
                # validation and completeness remain authoritative.
                fw=float(lm_cfg.get("overall_confidence_weight",0.03)) if self.learned_router and family_signal else 0.0
                overall_business = (
                    (0.22-fw/2) * source_conf + 0.18 * routing_confidence + 0.30 * float(po.overall_confidence)
                    + 0.18 * validation_conf + (0.12-fw/2) * completeness + fw * family_signal
                )
                result.quality.overall_confidence = round(max(0.0, min(1.0, overall_business)), 4)
                result.quality.requires_human_review = bool(po.requires_human_review or completeness < float(universal_cfg.get("business_completeness_review_threshold", 0.50)))
                lm_cfg=self.config.get("learned_models", {}) or {}
                ood=float(learned_pred.get("ood_score",0.0) or 0.0)
                if self.learned_router and ood >= float(lm_cfg.get("ood_review_threshold",0.72)):
                    result.quality.requires_human_review = True
                    penalty=float(lm_cfg.get("ood_confidence_penalty",0.08))*min(1.0,ood)
                    result.quality.overall_confidence=round(max(0.0,result.quality.overall_confidence-penalty),4)
                    result.quality.warnings.append(f"Learned layout router reports out-of-distribution risk={ood:.3f}.")

        # Preserve appended legal terms as a separate component instead of feeding
        # them into the transactional extractor. Generic raw text/blocks are still kept.
        legal_page_numbers = [int(x["page"]) for x in page_classifications if x.get("page") is not None and x.get("type") == "legal_terms"]
        if legal_page_numbers:
            page_text_lookup = {int(pg.get("page")): str(pg.get("text", "") or "") for pg in read.pages if pg.get("page") is not None}
            legal_text = "\n\n".join(page_text_lookup.get(n, "") for n in legal_page_numbers if page_text_lookup.get(n, ""))
            legal_conf = [float(x.get("confidence", 0.0)) for x in page_classifications if x.get("type") == "legal_terms"]
            result.business_extractions["terms_and_conditions"] = {
                "document_type": "purchase_terms" if primary_type == "purchase_order" else "legal_terms",
                "pages": legal_page_numbers,
                "page_count": len(legal_page_numbers),
                "confidence": round(sum(legal_conf) / max(1, len(legal_conf)), 4),
                "raw_text": legal_text[: int(self.config.get("universal", {}).get("max_text_chars", 500000))],
            }

        # Auto-discovered procurement ontology learned from public PO vocabulary.
        if primary_type == "purchase_order":
            try:
                lex_path = Path(__file__).resolve().parents[1] / "training" / "artifacts" / "alias_lexicon.json"
                if lex_path.exists():
                    lexicon = json.loads(lex_path.read_text(encoding="utf-8"))
                    # V4 merges aliases learned from human corrections without mutating
                    # the shipped public-corpus ontology.
                    feedback_cfg = self.config.get("feedback", {}) or {}
                    learned_alias_path = Path(str(feedback_cfg.get("learned_aliases_path", "training/feedback/learned_aliases.json")))
                    if not learned_alias_path.is_absolute():
                        learned_alias_path = Path(__file__).resolve().parents[1] / learned_alias_path
                    if feedback_cfg.get("learn_aliases", True) and learned_alias_path.exists():
                        try:
                            human_aliases = json.loads(learned_alias_path.read_text(encoding="utf-8"))
                            for field, aliases in human_aliases.items():
                                if not isinstance(aliases, list):
                                    continue
                                spec = lexicon.setdefault(field, {"aliases": []})
                                if not isinstance(spec, dict):
                                    spec = {"aliases": list(spec) if isinstance(spec, list) else []}
                                    lexicon[field] = spec
                                existing = list(spec.get("aliases", []))
                                for alias in aliases:
                                    if str(alias).strip() and alias not in existing:
                                        existing.append(alias)
                                spec["aliases"] = existing
                        except Exception as exc:
                            result.quality.warnings.append(f"Human alias memory unavailable: {exc}")
                    result.business_extractions["learned_ontology"] = extract_schema_fields(
                        result, lexicon,
                        min_score=float(self.config.get("universal", {}).get("schema_match_threshold", 0.72)),
                    )
            except Exception as exc:
                result.quality.warnings.append(f"Learned ontology unavailable: {exc}")

        if schema:
            result.business_extractions["requested_schema"] = extract_schema_fields(
                result,
                schema,
                min_score=float(self.config.get("universal", {}).get("schema_match_threshold", 0.72)),
            )

        # Routing/business safety gate: high OCR confidence cannot waive review when
        # a transactional document produced no structured business object.
        if universal_cfg.get("review_on_missing_business_extraction", True) and primary_type in {"purchase_order", "invoice", "delivery_note", "quotation"} and primary_type not in result.business_extractions:
            result.quality.requires_human_review = True
            result.quality.business_extraction_confidence = 0.0
            result.quality.completeness_confidence = 0.0
            result.quality.overall_confidence = round(min(result.quality.overall_confidence, 0.58), 4)
            result.quality.warnings.append(f"Transactional page(s) detected as {primary_type} but no structured business extraction was produced.")

        # V4 multimodal layer. It is additive: OCR/native evidence is preserved, then
        # fused with a vision-language model only when the configured trigger fires.
        semantic_cfg = dict(self.config.get("semantic_model", {}) or {})
        multimodal_cfg = dict(self.config.get("multimodal", {}) or {})
        if semantic_cfg.get("enabled") and "enabled" not in multimodal_cfg:
            multimodal_cfg["enabled"] = True
            multimodal_cfg.setdefault("mode", "always")
        fusion_cfg = dict(self.config.get("fusion", {}) or {})
        if multimodal_cfg.get("enabled"):
            try:
                report = run_multimodal(
                    result, str(p), multimodal_cfg=multimodal_cfg, backend_cfg=semantic_cfg,
                    fusion_cfg=fusion_cfg, schema=schema,
                )
                result.multimodal = report
                result.quality.multimodal_invoked = bool(report.invoked)
                result.quality.multimodal_confidence = float(report.confidence)
                result.quality.conflict_count = len(report.conflicts)
                result.quality.critical_conflict_count = sum(1 for c in report.conflicts if c.severity == "critical")
                if report.invoked:
                    result.business_extractions["multimodal_fused"] = report.fields
                    if report.unknown_fields:
                        result.business_extractions["vlm_unknown_fields"] = report.unknown_fields
                    if report.line_items:
                        result.business_extractions["vlm_line_items"] = report.line_items
                    if report.tables:
                        result.business_extractions["vlm_tables"] = report.tables
                    if report.warnings:
                        result.quality.warnings.extend(f"VLM: {w}" for w in report.warnings)
                    native_conf = float(result.quality.overall_confidence)
                    nw = float(fusion_cfg.get("native_weight", 0.72))
                    vw = float(fusion_cfg.get("vlm_weight", 0.28))
                    blended = (nw * native_conf + vw * float(report.confidence)) / max(1e-9, nw + vw)
                    max_boost = float(fusion_cfg.get("max_confidence_boost", 0.08))
                    result.quality.overall_confidence = round(min(native_conf + max_boost, blended), 4)
                    if result.quality.critical_conflict_count:
                        result.quality.overall_confidence = round(max(0.0, result.quality.overall_confidence - float(fusion_cfg.get("critical_conflict_penalty", 0.18))), 4)
                        result.quality.requires_human_review = True
                    elif report.conflicts and fusion_cfg.get("review_on_any_conflict", False):
                        result.quality.requires_human_review = True
                    elif report.confidence < float(multimodal_cfg.get("manual_review_threshold", 0.78)):
                        result.quality.requires_human_review = True
                    if report.raw_response.get("languages") and not result.document.detected_languages:
                        result.document.detected_languages = [str(x) for x in report.raw_response.get("languages", [])]
            except Exception as exc:
                result.quality.warnings.append(f"Multimodal model unavailable: {exc}")

        result.document.processing_duration_seconds = round(time.perf_counter() - started, 4)
        return result
