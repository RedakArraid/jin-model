from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
import uuid

import fitz

from .azure_backend import analyze_with_azure
from .confidence import score
from .config import load_config, deep_merge
from .extract import (all_rows, extract_header, extract_parties, extract_lines, extract_lines_from_pages, extract_totals, extract_extra_fields,
    extract_spatial_header_fields, enhance_parties_spatial, extract_multiline_lines_from_pages, enhance_totals_spatial,
    enhance_commercial_terms, extract_document_notes, extract_tax_summaries, choose_best_line_items)
from .ingest import describe_file, open_image, render_pdf_page
from .models import DocumentMeta, PageResult, PurchaseOrderResult, PurchaseOrderHeader
from .layout_guides import extract_vector_layout
from .ocr import tesseract_page_with_diagnostics
from .pdf_native import is_native_page, extract_native_page
from .preprocess import preprocess
from .rl_policy import PROFILES, classify_image_details, load_policy, ranked_actions, apply_profile
from .validate import validate


class PurchaseOrderOCR:
    def __init__(self, config_path: str | Path | None = None, overrides: dict | None = None):
        self.config = load_config(config_path)
        if overrides:
            self.config = deep_merge(self.config, overrides)
        rl_cfg = self.config.get("reinforcement_policy", {})
        policy_path = rl_cfg.get("path")
        if policy_path and not Path(policy_path).is_absolute():
            project_root = (Path(config_path).resolve().parent.parent if config_path else Path(__file__).resolve().parents[1])
            policy_path = project_root / policy_path
        self.rl_policy = load_policy(policy_path) if rl_cfg.get("enabled") else {}

    def extract(self, path: str | Path) -> PurchaseOrderResult:
        started = time.perf_counter()
        fd = describe_file(path)
        accepted = set(self.config["input"].get("accepted_extensions", []))
        if fd.extension not in accepted:
            raise ValueError(f"Unsupported extension: {fd.extension}")
        max_bytes = int(self.config["input"].get("max_file_size_mb", 100)) * 1024 * 1024
        if fd.file_size > max_bytes:
            raise ValueError("Input file exceeds configured max_file_size_mb")

        backend = self.config["engine"].get("default_backend", "local")
        if backend == "azure" or self.config.get("azure_document_intelligence", {}).get("enabled"):
            pages = analyze_with_azure(path, self.config["azure_document_intelligence"])
            page_kinds = ["azure"] * len(pages)
        else:
            pages, page_kinds = self._local_pages(path, fd.extension)

        rows = all_rows(pages, y_factor=float(self.config["layout"].get("line_y_tolerance_factor", 0.65)))
        # A slightly larger row tolerance joins labels with values when OCR puts them on
        # nearby baselines (common in forms with bordered cells).
        header_rows = all_rows(pages, y_factor=max(0.9, float(self.config["layout"].get("line_y_tolerance_factor", 0.65))))
        header_fields, commercial, logistics = extract_header(header_rows, self.config["normalization"].get("default_currency", "EUR"))
        header_fields = extract_spatial_header_fields(pages, header_fields) if self.config["layout"].get("spatial_header_recovery", True) else header_fields
        currency = (header_fields.get("currency").value if header_fields.get("currency") else None) or self.config["normalization"].get("default_currency", "EUR")
        parties = extract_parties(header_rows)
        if self.config["layout"].get("spatial_party_segmentation", True):
            parties = enhance_parties_spatial(pages, parties)
        commercial, logistics = enhance_commercial_terms(header_rows, commercial, logistics)
        min_headers = int(self.config["layout"].get("min_table_header_matches", 3))
        segmented_lines = extract_multiline_lines_from_pages(pages, default_currency=currency, min_header_matches=min_headers) if self.config["layout"].get("multiline_item_segmentation", True) else []
        spatial_lines = extract_lines_from_pages(pages, default_currency=currency, min_header_matches=min_headers)
        legacy_lines = extract_lines(rows, default_currency=currency, min_header_matches=min_headers)
        lines = choose_best_line_items(segmented_lines, spatial_lines, legacy_lines)
        totals = extract_totals(header_rows, default_currency=currency)
        if self.config["layout"].get("spatial_totals_recovery", True):
            totals = enhance_totals_spatial(pages, totals, default_currency=currency)

        po_header = PurchaseOrderHeader()
        for key, value in header_fields.items():
            if hasattr(po_header, key):
                setattr(po_header, key, value)

        known_values = [f.raw_value for f in header_fields.values()]
        known_values.extend([p.name for p in parties.values()])
        extras = extract_extra_fields(header_rows, known_values)

        now = datetime.now(timezone.utc).isoformat()
        is_pdf = fd.extension == "pdf"
        is_image = not is_pdf
        is_native = is_pdf and bool(page_kinds) and all(k == "native_pdf" for k in page_kinds)
        is_scan = (is_pdf and bool(page_kinds) and all(k == "scan_ocr" for k in page_kinds)) or is_image
        is_mixed = is_pdf and len(set(page_kinds)) > 1
        has_local_ocr = any(word.source == "ocr" for page in pages for word in page.words)

        meta = DocumentMeta(
            id=str(uuid.uuid4()), filename=fd.filename, mime_type=fd.mime_type, extension=fd.extension,
            file_size=fd.file_size, sha256=fd.sha256, page_count=len(pages), is_pdf=is_pdf, is_image=is_image,
            is_scan=is_scan, is_native_pdf=is_native, is_mixed_pdf=is_mixed,
            has_text_layer=is_native or is_mixed, has_images=is_scan or is_mixed or is_image or has_local_ocr,
            ocr_required=any(k in {"scan_ocr", "image_ocr"} for k in page_kinds) or has_local_ocr,
            ocr_engine=self.config["ocr"].get("engine") if has_local_ocr or any(k in {"scan_ocr", "image_ocr"} for k in page_kinds) else None,
            processing_timestamp=now, processing_duration_seconds=0.0,
            model_version=self.config["engine"].get("version", "0.1.0"),
        )

        result = PurchaseOrderResult(
            document=meta, purchase_order=po_header,
            buyer=parties.get("buyer"), supplier=parties.get("supplier"),
            ship_to=parties.get("ship_to"), bill_to=parties.get("bill_to"),
            commercial=commercial, logistics=logistics, lines=lines, totals=totals, taxes=extract_tax_summaries(header_rows, totals), notes=extract_document_notes(header_rows),
            extra_fields=extras, pages=pages if self.config["engine"].get("keep_page_words", True) else [],
        )
        result = validate(result, self.config["validation"])
        result = score(result, {**self.config["confidence"], "manual_review_threshold": self.config["ocr"].get("manual_review_threshold", 0.80)})
        result.document.processing_duration_seconds = round(time.perf_counter() - started, 4)
        return result

    def _ocr_image(self, image, page_no: int, source_type: str) -> PageResult:
        """Run adaptive preprocessing/OCR and, when useful, evaluate several RL actions."""
        rl_cfg = self.config.get("reinforcement_policy", {})
        state, state_conf, quality_metrics = classify_image_details(image)
        base_pre, base_ocr = self.config["preprocessing"], self.config["ocr"]

        if self.rl_policy:
            requested = max(
                int(rl_cfg.get("top_k", 1)),
                int(rl_cfg.get("max_actions_to_evaluate", 1)) if rl_cfg.get("evaluate_multiple_actions_on_low_quality", True) else 1,
            )
            actions = ranked_actions(
                self.rl_policy,
                state,
                top_k=requested,
                fallback=str(rl_cfg.get("fallback_action", "balanced")),
            )
            override_thresholds = rl_cfg.get("state_override_min_confidence", {}) or {}
            state_threshold = float(override_thresholds.get(state, rl_cfg.get("min_state_confidence", 0.45)))
            if rl_cfg.get("state_override_enabled", False) and state in PROFILES and state_conf >= state_threshold:
                actions = [state] + [a for a in actions if a != state]
        else:
            actions = [str(rl_cfg.get("fallback_action", "balanced"))]

        results = []
        action_started = time.perf_counter()
        action_budget = float(self.config.get("performance", {}).get("max_seconds_per_page", 100))
        max_actions = max(1, int(rl_cfg.get("max_actions_to_evaluate", 3)))
        trigger = float(rl_cfg.get("multi_action_trigger_quality", 0.70))
        multi = bool(rl_cfg.get("evaluate_multiple_actions_on_low_quality", True))

        for idx, action in enumerate(actions[:max_actions]):
            if idx > 0 and (time.perf_counter() - action_started) >= action_budget:
                break
            pre_cfg, ocr_cfg = apply_profile(base_pre, base_ocr, action)
            ocr_cfg["page_time_budget_seconds"] = min(float(ocr_cfg.get("page_time_budget_seconds", 90)), max(5.0, action_budget - (time.perf_counter() - action_started)))
            processed, rotation = preprocess(image, pre_cfg)
            text, words, conf, diag = tesseract_page_with_diagnostics(processed, page_no, ocr_cfg)
            q = float(diag.get("selected_quality", conf))
            results.append((q, conf, action, processed, rotation, text, words, diag))
            # Fast path: one strong reading is better than spending time on redundant passes.
            if idx == 0 and (not multi or q >= trigger):
                break

        results.sort(key=lambda x: (x[0], x[1], len(x[6])), reverse=True)
        q, conf, action, processed, rotation, text, words, diag = results[0]
        diag = dict(diag)
        diag["image_state"] = state
        diag["image_state_confidence"] = round(state_conf, 4)
        diag["selected_action"] = action
        diag["evaluated_actions"] = [
            {"action": r[2], "quality": round(r[0], 4), "confidence": round(r[1], 4), "words": len(r[6])}
            for r in results
        ]
        page_result = PageResult(
            page=page_no,
            width=processed.width,
            height=processed.height,
            source_type=source_type,
            text=text,
            words=words,
            confidence=conf,
            rotation_applied=rotation,
            ocr_quality=q,
            image_state=state,
            image_state_confidence=state_conf,
            selected_ocr_action=action,
            ocr_diagnostics=diag if self.config.get("engine", {}).get("keep_ocr_diagnostics", True) else {},
            image_quality_metrics=quality_metrics if self.config.get("engine", {}).get("keep_image_quality_metrics", True) else {},
        )
        if self.config.get("layout", {}).get("verify_compact_order_header", True):
            from .extract import verify_compact_header_number
            verify_compact_header_number(page_result, processed, self.config["ocr"])
        return page_result

    def _local_pages(self, path: str | Path, extension: str) -> tuple[list[PageResult], list[str]]:
        pages: list[PageResult] = []
        kinds: list[str] = []
        if extension == "pdf":
            doc = fitz.open(path)
            max_pages = int(self.config["input"].get("max_pages", 250))
            if len(doc) > max_pages:
                raise ValueError("PDF exceeds configured max_pages")
            for index, page in enumerate(doc):
                page_no = index + 1
                if is_native_page(page, self.config["pdf"]):
                    text, words, conf = extract_native_page(page, page_no, self.config["pdf"])
                    vector_lines, vector_rectangles = extract_vector_layout(page, self.config.get("layout", {}))
                    pages.append(PageResult(page=page_no, width=page.rect.width, height=page.rect.height,
                                            source_type="native_pdf", text=text, words=words, confidence=conf,
                                            ocr_quality=conf,
                                            ocr_diagnostics={"hybrid_ocr_words": sum(w.source == "ocr" for w in words)},
                                            vector_lines=vector_lines, vector_rectangles=vector_rectangles))
                    kinds.append("native_pdf")
                else:
                    image = render_pdf_page(page, dpi=int(self.config["pdf"].get("render_dpi", 300)))
                    pages.append(self._ocr_image(image, page_no, "scan_ocr"))
                    kinds.append("scan_ocr")
            doc.close()
        else:
            image = open_image(path)
            pages.append(self._ocr_image(image, 1, "image_ocr"))
            kinds.append("image_ocr")
        return pages, kinds
