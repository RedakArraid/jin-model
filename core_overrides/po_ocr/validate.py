from __future__ import annotations

from datetime import date
from collections import Counter

from .models import PurchaseOrderResult


def _close(a: float, b: float, abs_tol: float, rel_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b), 1.0))


def _currency_of_header(result: PurchaseOrderResult) -> str | None:
    v = result.purchase_order.currency.normalized_value or result.purchase_order.currency.value
    return str(v).upper() if v else None



def _fold_semantic(value: str | None) -> str:
    import unicodedata
    text=unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode("ascii").casefold()
    return " ".join(text.split())


def _valid_po_number_type(value: str | None) -> bool:
    import re
    text=" ".join(str(value or "").split()).strip()
    low=_fold_semantic(text)
    if not text or "@" in text or ":" in text: return False
    if any(x in low for x in ("code fournisseur","supplier code","vendor code","telephone","tel ","fax","telecopie","tva","siret","iban","total","poids","volume")):
        return False
    if re.fullmatch(r"\+?\d{1,3}(?:[ .-]\d{2,4}){2,}",text): return False
    if re.fullmatch(r"(?:\d{2}[ .-]){4,}\d{2}",text): return False
    # Alpha-only order identifiers exist in some ERPs; semantic rejection focuses on
    # role/type contamination rather than an arbitrary digit quota.
    if not any(ch.isalnum() for ch in text): return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{1,63}",text))


def _party_name_semantic_quality(value: str | None) -> float:
    import re
    text=" ".join(str(value or "").split()).strip()
    low=_fold_semantic(text)
    if not text: return 0.0
    if "@" in text: return 0.0
    if re.fullmatch(r"[+0-9 .()/-]{7,}",text): return 0.0
    if any(x in low for x in ("telephone","fax","telecopie","code fournisseur","date","reference","produit fini","mode livraison")):
        return 0.1
    if any(x in low for x in ("retourner sous","merci de livrer","suivant offre","franco","adresse de livraison","a facturer","a livrer","bon de commande","commande n","total ht","net a payer")):
        return 0.05
    if re.match(r"^(?:rue|avenue|route|impasse|boulevard|chemin|zac|zae|zi|z\.i\.)\b",low): return 0.08
    if re.match(r"^\d+\s+(?:rue|avenue|route|impasse|boulevard|chemin)\b",low): return 0.15
    if re.fullmatch(r"\d{5}\s+.+",text): return 0.15
    alpha=sum(ch.isalpha() for ch in text)
    if alpha<3: return 0.2
    return min(1.0,0.55+alpha/max(10,len(text))*0.45)

def validate(result: PurchaseOrderResult, cfg: dict) -> PurchaseOrderResult:
    checks: dict[str, object] = {}
    warnings: list[str] = []
    errors: list[str] = []
    abs_tol = float(cfg.get("amount_absolute_tolerance", 0.03))
    rel_tol = float(cfg.get("amount_relative_tolerance", 0.005))

    valid_line_amounts = 0
    checked_line_amounts = 0
    if cfg.get("quantity_price_check", True):
        for i, line in enumerate(result.lines):
            if line.quantity is not None and line.unit_price is not None and line.line_total is not None:
                checked_line_amounts += 1
                expected = line.quantity * line.unit_price
                if _close(expected, line.line_total, abs_tol, rel_tol):
                    valid_line_amounts += 1
                    line.validation_status = "VALID"
                else:
                    line.validation_status = "INVALID"
                    msg = f"Line {line.line_number or i+1}: quantity * unit_price != line_total ({expected:.2f} vs {line.line_total:.2f})"
                    line.warnings.append(msg)
                    warnings.append(msg)
        checks["quantity_price"] = {"checked": checked_line_amounts, "valid": valid_line_amounts,
                                    "ratio": valid_line_amounts / max(1, checked_line_amounts)}

    if cfg.get("negative_quantity_warning", True):
        bad = [x.line_number or str(i+1) for i, x in enumerate(result.lines) if x.quantity is not None and x.quantity < 0]
        checks["negative_quantities"] = {"count": len(bad), "lines": bad}
        if bad:
            warnings.append(f"Negative quantities detected on line(s): {', '.join(bad)}")

    if cfg.get("negative_price_warning", True):
        bad = [x.line_number or str(i+1) for i, x in enumerate(result.lines) if x.unit_price is not None and x.unit_price < 0]
        checks["negative_prices"] = {"count": len(bad), "lines": bad}
        if bad:
            warnings.append(f"Negative unit prices detected on line(s): {', '.join(bad)}")

    if cfg.get("line_sum_check", True) and result.lines:
        amounts = [x.line_total for x in result.lines if x.line_total is not None]
        if amounts:
            product_sum = sum(amounts)
            charge_amounts = [x.amount for x in getattr(result, "additional_charges", []) if x.amount is not None]
            charge_sum = sum(charge_amounts) if charge_amounts else 0.0
            document_sum = product_sum + charge_sum
            result.totals.calculated_line_total = round(product_sum, 6)
            result.totals.calculated_additional_charges = round(charge_sum, 6) if charge_amounts else 0.0
            result.totals.calculated_document_net = round(document_sum, 6)
            result.totals.calculated_line_total_confidence = sum(float(x.confidence or 0.0) for x in result.lines if x.line_total is not None) / max(1, len(amounts))
            target = result.totals.total_net if result.totals.total_net is not None else result.totals.subtotal
            if target is not None:
                product_ok = _close(product_sum, target, abs_tol, rel_tol)
                document_ok = _close(document_sum, target, abs_tol, rel_tol)
                ok = document_ok if charge_amounts else product_ok
                checks["line_sum"] = {
                    "product_sum": product_sum, "additional_charges": charge_sum,
                    "document_sum": document_sum, "target": target, "valid": ok,
                    "product_only_valid": product_ok, "including_charges_valid": document_ok,
                }
                if not ok:
                    warnings.append(f"Product lines + additional charges ({document_sum:.2f}) differ from net/subtotal ({target:.2f})")
            else:
                checks["line_sum"] = {
                    "product_sum": product_sum, "additional_charges": charge_sum,
                    "document_sum": document_sum, "target": None,
                    "status": "computed_only", "valid": None,
                }

    if cfg.get("additional_charge_arithmetic_check", True) and getattr(result, "additional_charges", None):
        checked = 0
        valid = 0
        for idx, charge in enumerate(result.additional_charges):
            if charge.quantity is None or charge.unit_price is None or charge.amount is None:
                continue
            checked += 1
            expected = charge.quantity * charge.unit_price
            if _close(expected, charge.amount, abs_tol, rel_tol):
                valid += 1
            else:
                warnings.append(f"Additional charge {idx+1}: quantity * unit_price != amount ({expected:.2f} vs {charge.amount:.2f})")
        checks["additional_charge_arithmetic"] = {"checked": checked, "valid": valid, "ratio": valid / max(1, checked)}

    if cfg.get("tax_check", True):
        net = result.totals.total_net if result.totals.total_net is not None else result.totals.subtotal
        tax = result.totals.total_tax if result.totals.total_tax is not None else result.totals.total_vat
        gross = result.totals.total_gross if result.totals.total_gross is not None else result.totals.total_after_tax
        if net is not None and tax is not None and gross is not None:
            ok = _close(net + tax, gross, abs_tol, rel_tol)
            checks["tax_total"] = {"net": net, "tax": tax, "gross": gross, "valid": ok}
            if not ok:
                warnings.append(f"Net + tax ({net + tax:.2f}) differs from gross total ({gross:.2f})")

    if cfg.get("grand_total_check", True) and result.totals.grand_total is not None:
        base = result.totals.total_net if result.totals.total_net is not None else result.totals.subtotal
        if base is not None:
            raw_components = {
                "tax": result.totals.total_tax if result.totals.total_tax is not None else result.totals.total_vat,
                "freight": result.totals.total_freight,
                "shipping": result.totals.total_shipping,
                "surcharge": result.totals.total_surcharge,
                "discount": result.totals.total_discount,
                "rounding": result.totals.rounding,
            }
            has_component = any(v is not None for v in raw_components.values())
            if cfg.get("grand_total_require_components", True) and not has_component:
                checks["grand_total"] = {"status": "not_checked", "reason": "insufficient_components", "base": base, "target": result.totals.grand_total}
            else:
                components = {"base": base, **{k: (v or 0.0) for k,v in raw_components.items()}}
                expected = components["base"] + components["tax"] + components["freight"] + components["shipping"] + components["surcharge"] - components["discount"] + components["rounding"]
                ok = _close(expected, result.totals.grand_total, abs_tol, rel_tol)
                checks["grand_total"] = {"expected": expected, "target": result.totals.grand_total, "valid": ok, "components": components}
                if not ok:
                    warnings.append(f"Computed grand total ({expected:.2f}) differs from extracted grand total ({result.totals.grand_total:.2f})")

    order_date = result.purchase_order.order_date.normalized_value
    delivery_date = result.purchase_order.expected_delivery_date.normalized_value
    if cfg.get("date_consistency_check", True) and order_date and delivery_date:
        try:
            ok = date.fromisoformat(order_date) <= date.fromisoformat(delivery_date)
            checks["date_consistency"] = ok
            if not ok:
                warnings.append("Order date is after expected delivery date")
        except ValueError:
            checks["date_consistency"] = "unparseable"

    if cfg.get("currency_consistency_check", True):
        currencies = []
        header_currency = _currency_of_header(result)
        if header_currency:
            currencies.append(("header", header_currency))
        if result.totals.currency:
            currencies.append(("totals", str(result.totals.currency).upper()))
        for i, line in enumerate(result.lines):
            if line.currency:
                currencies.append((f"line:{line.line_number or i+1}", str(line.currency).upper()))
        unique = sorted({c for _, c in currencies})
        ok = len(unique) <= 1
        checks["currency_consistency"] = {"currencies": unique, "valid": ok}
        if not ok:
            warnings.append(f"Multiple currencies detected: {', '.join(unique)}")

    if cfg.get("line_number_sequence_check", True) and result.lines:
        nums=[]
        for line in result.lines:
            try:
                nums.append(int(str(line.line_number)))
            except Exception:
                continue
        if len(nums) >= 2:
            increasing = all(b > a for a,b in zip(nums, nums[1:]))
            checks["line_number_sequence"] = {"values": nums, "valid": increasing}
            if not increasing:
                warnings.append("Line numbers are not strictly increasing")

    if cfg.get("duplicate_line_warning", True) and result.lines:
        signatures=[]
        for line in result.lines:
            sig=(line.material_number or line.article_number or line.product_code, line.description, line.quantity, line.unit_price, line.line_total)
            if any(v is not None for v in sig):
                signatures.append(sig)
        dup=[sig for sig,count in Counter(signatures).items() if count>1]
        checks["duplicate_lines"]={"count":len(dup),"valid":not dup}
        if dup:
            warnings.append(f"Potential duplicate line items detected: {len(dup)} signature(s)")

    # V4.7 semantic type guards: a populated field is not considered valid merely
    # because OCR confidently produced characters.
    if cfg.get("po_number_type_guard_check", True):
        po_value = result.purchase_order.number.value
        po_valid = _valid_po_number_type(str(po_value)) if po_value is not None else False
        checks["purchase_order_number_type"] = {"value": po_value, "valid": po_valid}
        if po_value is not None and not po_valid:
            msg = f"Purchase-order number has an invalid semantic type: {po_value!r}"
            if cfg.get("critical_failure_on_invalid_po_number", True): errors.append(msg)
            else: warnings.append(msg)

    if cfg.get("po_number_contact_collision_check", True):
        import re
        po_value=result.purchase_order.number.value
        def digits(v):
            return re.sub(r"\D","",str(v or ""))
        po_digits=digits(po_value)
        collisions=[]
        if po_digits and str(po_value or "").strip().replace(" ","").isdigit():
            for role,party in (("buyer",result.buyer),("supplier",result.supplier),("ship_to",result.ship_to),("bill_to",result.bill_to)):
                for kind,val in (("phone",getattr(party,"phone",None)),("fax",getattr(party,"fax",None)),
                                 ("contact_phone",getattr(getattr(party,"contact",None),"phone",None)),
                                 ("contact_fax",getattr(getattr(party,"contact",None),"fax",None))):
                    dv=digits(val)
                    if dv and len(dv)>=7 and dv==po_digits:
                        collisions.append({"role":role,"field":kind,"value":val})
        valid=not collisions
        checks["purchase_order_contact_collision"]={"value":po_value,"valid":valid,"collisions":collisions}
        if collisions:
            msg=f"Purchase-order number collides with a business-party contact number: {po_value!r}"
            if cfg.get("critical_failure_on_po_contact_collision",True): errors.append(msg)
            else: warnings.append(msg)

    if cfg.get("party_role_semantic_check", True):
        role_values={"buyer":result.buyer.name,"supplier":result.supplier.name,"ship_to":result.ship_to.name}
        role_checks={}
        for role,value in role_values.items():
            quality=_party_name_semantic_quality(value)
            role_checks[role]={"value":value,"quality":round(quality,4),"valid":quality>=float(cfg.get("party_name_min_semantic_quality",0.45))}
        checks["party_role_semantics"]=role_checks
        critical_roles=list(cfg.get("critical_party_roles",["buyer","supplier"]))
        raw_low=_fold_semantic("\n".join(p.text for p in result.pages if getattr(p,"text",None)))
        if cfg.get("conditional_role_semantic_check",True):
            role_triggers={
                "ship_to": ("adresse de livraison","a livrer a","livre a","ship to"),
                "bill_to": ("facture a","a facturer a","bill to"),
            }
            for role,triggers in role_triggers.items():
                if any(t in raw_low for t in triggers):
                    party=getattr(result,role,None)
                    quality=_party_name_semantic_quality(getattr(party,"name",None))
                    valid=quality>=float(cfg.get("explicit_role_name_min_semantic_quality",0.45))
                    role_checks[role]={"value":getattr(party,"name",None),"quality":round(quality,4),"valid":valid,"explicit_label_present":True}
                    if role not in critical_roles: critical_roles.append(role)
        bad=[r for r in critical_roles if r in role_checks and not role_checks[r]["valid"]]
        if bad:
            warnings.append("Semantically implausible/missing business party role(s): "+", ".join(bad))

    if cfg.get("line_field_completeness_check", True) and result.lines:
        raw_text="\n".join(p.text for p in result.pages if getattr(p,"text",None))
        low=_fold_semantic(raw_text)
        qty_header=any(x in low for x in ("quantite","qte","qty"))
        # Dotted ERP headings denote the same unit-price column as "PU HT".
        import re
        price_header = any(x in low for x in ("prix net", "prix unitaire", "unit price")) or bool(
            re.search(r"\bp\s*\.?\s*u\s*\.?\s+h\s*\.?\s*t\b", low)
        )
        amount_header=any(x in low for x in ("montant ht","montant net","line total","amount"))
        n=len(result.lines)
        qty_ratio=sum(x.quantity is not None for x in result.lines)/n
        price_ratio=sum(x.unit_price is not None for x in result.lines)/n
        amount_ratio=sum(x.line_total is not None for x in result.lines)/n
        valid=True
        min_ratio=float(cfg.get("line_required_field_min_ratio",0.80))
        if qty_header and qty_ratio<min_ratio: valid=False
        if price_header and price_ratio<min_ratio: valid=False
        if amount_header and amount_ratio<min_ratio: valid=False
        checks["line_field_completeness"]={"quantity_ratio":qty_ratio,"unit_price_ratio":price_ratio,"line_total_ratio":amount_ratio,"quantity_header":qty_header,"price_header":price_header,"amount_header":amount_header,"valid":valid}
        if not valid:
            warnings.append("Structured line items are missing fields that are explicitly present in the document table header")

    if cfg.get("printed_total_presence_check", True):
        from .extract import abbreviated_net_total
        raw_text="\n".join(p.text for p in result.pages if getattr(p,"text",None))
        low=_fold_semantic(raw_text)
        printed_total=any(x in low for x in ("total ht","total h.t","total net ht","net a payer")) or bool(abbreviated_net_total(raw_text))
        extracted_total=result.totals.total_net is not None or result.totals.subtotal is not None or result.totals.grand_total is not None
        checks["printed_total_presence"]={"printed_label_present":printed_total,"extracted":extracted_total,"valid":(not printed_total) or extracted_total}
        if printed_total and not extracted_total:
            warnings.append("A printed financial total is visible but no structured total was extracted")

    if cfg.get("legal_footer_line_item_check",True) and result.lines:
        legal_terms=("siren","siret","rcs","iban","bic","capital de","capital social","intracom","ape ","naf ")
        bad=[]
        for idx,line in enumerate(result.lines):
            low=_fold_semantic(" ".join(str(x or "") for x in (line.material_number,line.description,line.raw_text)))
            if any(t in low for t in legal_terms): bad.append(line.line_number or str(idx+1))
        checks["legal_footer_line_items"]={"count":len(bad),"lines":bad,"valid":not bad}
        if bad: warnings.append("Legal/footer text was structured as purchase-order line item(s): "+", ".join(bad))

    if not result.purchase_order.number.value and cfg.get("critical_failure_on_missing_po_number", True):
        errors.append("Missing purchase order number")
    if not result.lines:
        if cfg.get("critical_failure_on_no_lines", False):
            errors.append("No structured line items detected")
        else:
            warnings.append("No structured line items detected")

    status = "FAIL" if errors else ("WARNING" if warnings else "PASS")
    result.validation.status = status
    result.validation.checks = checks
    result.validation.warnings = warnings
    result.validation.errors = errors
    return result
