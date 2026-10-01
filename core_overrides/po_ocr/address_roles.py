from __future__ import annotations

import hashlib
import re
from typing import Iterable

from .models import Address, BusinessAddress, Contact, Evidence, PageResult, Party
from .normalize import ascii_fold, clean_text
from .extract import all_rows
from .address_intelligence import parse_address, local_validation, verify_french_address
from .layout_guides import split_items_by_compartments


ROLE_LABELS_FR = {
    "buyer": "Acheteur / donneur d'ordre",
    "ordering_party": "Donneur d'ordre",
    "supplier": "Fournisseur",
    "sold_to": "Client vendu à / Sold-to",
    "bill_to": "Adresse de facturation",
    "ship_to": "Adresse de livraison",
    "deliver_to": "Destinataire de livraison",
    "invoice_to": "Destinataire de facture",
    "payer": "Payeur",
    "end_customer": "Client final",
    "consignee": "Consignataire / réceptionnaire",
    "ship_from": "Site expéditeur",
}

# Roles where an explicit business label normally exists in transactional documents.
EXPLICIT_ROLE_CONFIDENCE = {
    "ship_to": 0.98,
    "deliver_to": 0.97,
    "bill_to": 0.98,
    "invoice_to": 0.97,
    "payer": 0.96,
    "supplier": 0.96,
    "buyer": 0.95,
    "sold_to": 0.94,
    "end_customer": 0.94,
    "consignee": 0.95,
    "ship_from": 0.95,
    "ordering_party": 0.94,
}


def _fold(value: str | None) -> str:
    return " ".join(ascii_fold(value or "").casefold().split())


def _nonempty_address(addr: Address | None) -> bool:
    if addr is None:
        return False
    return any(getattr(addr, f, None) for f in ("line1", "line2", "line3", "street", "postal_code", "city", "country"))


def _party_has_content(p: Party | None) -> bool:
    if p is None:
        return False
    return bool(p.name or p.code or _nonempty_address(p.address) or p.email or p.phone or p.contact.name)


def normalize_address(addr: Address, config: dict | None = None) -> Address:
    """V5.1 component-aware address normalization.

    The parser keeps raw source lines, separates postal routing complements (BP/TSA/CS),
    CEDEX, street components, zones/buildings and only infers country with an explicit
    lower-confidence provenance handled by the business-address layer.
    """
    cfg = (config or {}).get("addresses", {}) if isinstance(config, dict) else {}
    infer_country = bool(cfg.get("infer_country_from_postal_pattern", True))
    out, _component_conf, _warnings = parse_address(addr, infer_country=infer_country)
    return out


def formatted_address(addr: Address) -> str | None:
    """Build a complete normalized postal label from typed components.

    The raw source remains available in ``address.raw``/``raw_lines``; this function
    intentionally emits the normalized, display-ready address without truncating to 3 lines.
    """
    parts: list[str] = []
    def add(value):
        v=clean_text(value)
        if v and _fold(v) not in {_fold(x) for x in parts}: parts.append(v)
    add(addr.building); add(addr.residence)
    if addr.street:
        if addr.street_name and re.match(r"^\d+(?:ERE|ER|EME|ÈME)$", _fold(addr.street_name).upper()):
            add(f"{addr.street_name} {addr.street_type or ''}".strip())
        else:
            add(" ".join(x for x in (addr.house_number, addr.street) if x))
    elif addr.line1:
        add(addr.line1)
    add(addr.industrial_zone); add(addr.business_park); add(addr.lieu_dit); add(addr.address_complement)
    add(addr.po_box); add(addr.tsa); add(addr.cs); add(addr.postal_routing_code)
    locality=" ".join(x for x in (addr.postal_code, addr.city) if x)
    if addr.cedex:
        locality=(locality + " CEDEX" + (f" {addr.cedex_number}" if addr.cedex_number else "")).strip()
    add(locality)
    add(addr.country)
    if not parts:
        for value in (addr.line1,addr.line2,addr.line3): add(value)
    return ", ".join(parts) if parts else None


def address_fingerprint(addr: Address) -> str | None:
    formatted = formatted_address(addr)
    if not formatted:
        return None
    canonical = re.sub(r"[^a-z0-9]", "", _fold(formatted))
    if not canonical:
        return None
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _address_confidence(addr: Address, component_confidence: dict[str, float] | None = None) -> float:
    component_confidence = component_confidence or {}
    status, structural_score, _warnings = local_validation(addr, component_confidence)
    # Structural score dominates; completeness is a smaller factor.
    essential = [bool(addr.postal_code), bool(addr.city), bool(addr.street or addr.lieu_dit or addr.industrial_zone)]
    completeness = sum(essential) / len(essential)
    conf = 0.78 * structural_score + 0.22 * completeness
    if status == "INVALID_OR_INCOMPLETE":
        conf = min(conf, 0.59)
    return round(max(0.0, min(0.99, conf)), 4)


def _find_evidence(pages: list[PageResult], party: Party, addr: Address) -> Evidence:
    candidates: list[str] = []
    if party.name:
        candidates.append(party.name)
    if addr.postal_code:
        candidates.append(addr.postal_code)
    if addr.line1:
        candidates.append(addr.line1)
    folded_candidates = [_fold(x) for x in candidates if _fold(x)]
    best = None
    best_score = -1
    for row in all_rows(pages, y_factor=0.58):
        low = _fold(row.text)
        score = 0
        for c in folded_candidates:
            if c and c in low:
                score += min(4, len(c.split()))
            elif c and len(c) >= 8 and any(tok in low for tok in c.split() if len(tok) >= 5):
                score += 1
        if score > best_score:
            best_score, best = score, row
    if best is not None and best_score > 0:
        return Evidence(page=best.page, bbox=best.bbox, source_text=best.text, extraction_method="business_address_role")
    return Evidence(page=pages[0].page if pages else None, source_text=formatted_address(addr), extraction_method="business_address_role")



ROLE_BLOCK_LABELS = {
    "bill_to": ["adresse de facturation", "adresse facturation", "billing address", "bill to", "adresse facture", "facture a expedier", "facture a"],
    "ship_to": ["adresse de livraison", "adresse livraison", "adresse destinataire", "shipping address", "ship to", "a livrer", "à livrer", "livrer a", "livrer à", "livraison souhaitée à", "lieu de livraison"],
    "supplier": ["adresse de commande fournisseur", "supplier address", "vendor address", "adresse fournisseur"],
    "buyer": ["adresse acheteur", "buyer address", "ordering address", "adresse donneur d'ordre"],
}

_ROLE_STOP_TOKENS = (
    "code client", "n° accord", "no accord", "date de livraison", "mode de règlement", "mode de reglement",
    "communication -", "designation", "désignation", "quantite", "quantité", "prix unitaire", "montant ligne",
    "conditions generales", "conditions générales", "total brut", "total net", "page ",
    "reliquat", "par vos soins", "livrer le",
)

_DEPARTMENT_TOKENS = (
    "comptabilite", "comptabilité", "controle factures", "contrôle factures", "service ", "departement", "département",
    "factures fournisseurs", "depot ",
)

def _split_visual_row(row, page: PageResult | None = None, gap_threshold: float = 48.0):
    groups = split_items_by_compartments(
        row.words,
        page.vector_lines if page else None,
        page.vector_rectangles if page else None,
        gap_threshold=gap_threshold,
    )
    return [(group["text"], group["bbox"], group["items"]) for group in groups]

def _looks_address_line(text: str) -> bool:
    low=_fold(text)
    if re.search(r"\b\d{5}\s+[a-z]", low): return True
    if re.search(r"^\s*\d{1,5}(?:\s*(?:-|&|/|bis|ter)\s*\d*)?\s+(?:rue|avenue|av\.?|boulevard|bd\.?|route|chemin|impasse|allee|allée|place|quai)\b", low): return True
    if re.search(r"\b(?:rue|avenue|boulevard|route|chemin|impasse|allee|allée|zac|zae|zone|z\.?i\.?|tsa|b\.?p\.?|c\.?s\.?|cedex|batiment|bâtiment|immeuble|residence|résidence)\b", low): return True
    return False

def _looks_metadata_line(text: str) -> bool:
    low=_fold(text)
    return bool(re.match(
        r"^(?:tel|telephone|fax|e[_ -]?mail|email|mail|contact|correspondant|a l'attention|attention|"
        r"infos?|consigne|code d'ouverture|horaires?|reception|en express|merci de)\b",
        low,
    ))

def _choose_party_and_address(lines: list[str]):
    clean=[clean_text(x) for x in lines if clean_text(x)]
    clean=[x for x in clean if not _looks_metadata_line(x)]
    if not clean: return None,None,[]
    first_addr=next((i for i,x in enumerate(clean) if _looks_address_line(x)), len(clean))
    pre=clean[:first_addr]
    addr=clean[first_addr:]
    party=None; department=[]
    if pre:
        nondept=[x for x in pre if not any(t in _fold(x) for t in _DEPARTMENT_TOKENS)]
        party=(nondept[-1] if nondept else pre[0])
        department=[x for x in pre if x != party]
    elif clean and not _looks_address_line(clean[0]):
        party=clean[0]; addr=clean[1:]
    return party," / ".join(department) or None,addr


def _label_word(text: str) -> str:
    return _fold(text).strip(" :;.-")


def _explicit_role_anchors(segments):
    """Match label words across adjacent baselines in their own narrow column."""
    words = [word for segment in segments for word in segment["words"]]
    anchors = []
    seen = set()
    for role, aliases in ROLE_BLOCK_LABELS.items():
        for alias in sorted(aliases, key=lambda value: len(value.split()), reverse=True):
            tokens = _fold(alias).split()
            for first in words:
                if _label_word(first.text) != tokens[0] or (role, id(first)) in seen:
                    continue
                matched = [first]
                for token in tokens[1:]:
                    previous = matched[-1]
                    height = max(5.0, previous.bbox[3] - previous.bbox[1])
                    candidates = []
                    for word in words:
                        if any(word is item for item in matched) or _label_word(word.text) != token:
                            continue
                        dx = word.bbox[0] - previous.bbox[2]
                        dy = word.bbox[1] - previous.bbox[1]
                        same_row = abs(dy) <= height * 0.55 and -1 <= dx <= 16
                        next_row = height * 0.6 < dy <= height * 2.0 and abs(word.bbox[0] - first.bbox[0]) <= 24
                        if same_row or next_row:
                            candidates.append(word)
                    if not candidates:
                        break
                    matched.append(min(candidates, key=lambda word: abs(word.bbox[1] - previous.bbox[1]) * 10 + abs(word.bbox[0] - previous.bbox[2])))
                if len(matched) != len(tokens):
                    continue
                seen.add((role, id(first)))
                bbox = (min(w.bbox[0] for w in matched), min(w.bbox[1] for w in matched),
                        max(w.bbox[2] for w in matched), max(w.bbox[3] for w in matched))
                anchors.append((role, {"text": alias, "bbox": bbox, "words": matched}))
    return anchors


_BLOCK_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_BLOCK_PHONE_RE = re.compile(r"(?<!\d)(?:\+33[ .-]?(?:\(0\)[ .-]?)?|0)[1-9](?:[ .-]?\d{2}){4}(?!\d)")


def _block_contacts(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Keep contact values with their labelled block, outside postal lines."""
    contacts: dict[str, str] = {}
    postal_lines = []
    for line in lines:
        contact_line = re.match(
            r"^(?:contact|correspondant|a\s+l['’ ]attention(?:\s+de)?|attention)\s*[:\-]?\s*(.+)$",
            clean_text(line), flags=re.I,
        )
        if contact_line:
            name = clean_text(contact_line.group(1)).strip(" :;-/")
            if name and "@" not in name and not _looks_address_line(name):
                contacts.setdefault("name", name)
        emails = _BLOCK_EMAIL_RE.findall(line)
        phones = list(_BLOCK_PHONE_RE.finditer(line))
        if emails:
            contacts.setdefault("email", emails[0])
        for index, match in enumerate(phones):
            prefix = _fold(line[:match.start()])
            key = "fax" if re.search(r"\bfax\s*[:/]?\s*$", prefix) else "phone"
            if "tel / fax" in prefix and index == 1:
                key = "fax"
            contacts.setdefault(key, match.group(0))
        if phones:
            name = line[:phones[0].start()].strip(" :;-/")
            name = re.sub(r"^(?:contact|a l'attention de|attention)\s*:?\s*", "", name, flags=re.I)
            if name and len(name.split()) <= 6 and not _looks_metadata_line(name) and not _looks_address_line(name):
                contacts.setdefault("name", name)
        if not emails and not phones and not contact_line and not _looks_metadata_line(line):
            postal_lines.append(line)
    return contacts, postal_lines


def _vector_bounds(page: PageResult, anchor_bbox, current_left: float, current_right: float, default_end_y: float):
    """Tighten a labelled block with real borders that surround its label."""
    ax1, ay1, ax2, ay2 = anchor_bbox
    center = (ax1 + ax2) / 2
    containing = [
        rect for rect in page.vector_rectangles
        if rect[0] - 2 <= center <= rect[2] + 2
        and rect[1] - 2 <= (ay1 + ay2) / 2 <= rect[3] + 2
        and rect[3] >= ay2 + 14
    ]
    if containing:
        cell = min(containing, key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1]))
        current_left = max(current_left, cell[0] - 1)
        current_right = min(current_right, cell[2] + 1)
        default_end_y = min(default_end_y, cell[3] - 1)

    verticals = [line for line in page.vector_lines if line.get("orientation") == "vertical"]
    left_guides = [
        float(line["x0"]) for line in verticals
        if float(line["x0"]) < center and float(line["y0"]) <= ay2 + 3 and float(line["y1"]) >= ay2 + 20
    ]
    right_guides = [
        float(line["x0"]) for line in verticals
        if float(line["x0"]) > center and float(line["y0"]) <= ay2 + 3 and float(line["y1"]) >= ay2 + 20
    ]
    if left_guides:
        current_left = max(current_left, max(left_guides) - 1)
    if right_guides:
        current_right = min(current_right, min(right_guides) + 1)

    horizontal_stops = [
        float(line["y0"]) for line in page.vector_lines
        if line.get("orientation") == "horizontal"
        and float(line["y0"]) >= ay2 + 14
        and float(line["x0"]) - 2 <= center <= float(line["x1"]) + 2
    ]
    if horizontal_stops:
        default_end_y = min(default_end_y, min(horizontal_stops) - 1)
    return current_left, current_right, default_end_y

def recover_explicit_role_blocks(result, pages: list[PageResult]):
    """Recover labelled address blocks using page geometry.

    This is intentionally generic: it relies on explicit business-role labels and column
    geometry rather than supplier/customer names or document templates.
    """
    rows=all_rows(pages,y_factor=0.46)
    for page in pages:
        prows=[r for r in rows if r.page==page.page]
        segments=[]
        for row in prows:
            for text,bbox,words in _split_visual_row(row, page):
                segments.append({"text":text,"bbox":bbox,"words":words})
        anchors = _explicit_role_anchors(segments)
        if not anchors: continue
        # Group labels on the same header line; their x positions define column boundaries.
        for role,anchor in anchors:
            ax1,ay1,ax2,ay2=anchor["bbox"]
            label_ids = {id(word) for _, label in anchors for word in label["words"]}
            label_height = max(w.bbox[3] - w.bbox[1] for w in anchor["words"])
            sidebar = ay2 - ay1 > 1.5 * label_height
            same=[(r,s) for r,s in anchors if abs(s["bbox"][1]-ay1) <= 12]
            centers=sorted([(s["bbox"][0]+s["bbox"][2])/2 for _,s in same])
            center=(ax1+ax2)/2
            left=0.0; right=float(page.width or 10000)
            lower=[c for c in centers if c<center]; upper=[c for c in centers if c>center]
            if lower: left=(max(lower)+center)/2
            elif len(same)==1: left=max(0.0,ax1-30)
            if upper: right=(min(upper)+center)/2
            elif len(same)==1:
                page_w=float(page.width or ax2+420)
                if center < page_w * 0.5:
                    right=min(page_w * 0.56, ax2+420)
                else:
                    right=min(page_w, ax2+420)
                    left=max(left, page_w * 0.44)
            stop_candidates=[]
            for seg2 in segments:
                ystop=seg2["bbox"][1]; low2=_fold(seg2["text"])
                if ystop > ay1 + 20 and any(tok in low2 for tok in _ROLE_STOP_TOKENS):
                    stop_candidates.append(ystop)
            for _, other in anchors:
                ox1, oy1, ox2, _ = other["bbox"]
                if oy1 > ay2 + 6 and left <= (ox1 + ox2) / 2 <= right:
                    stop_candidates.append(oy1)
            end_y=min(stop_candidates) - 2 if stop_candidates else ay2 + 220
            left, right, end_y = _vector_bounds(page, anchor["bbox"], left, right, end_y)
            collected=[]
            for seg in segments:
                # Do not clip the label off a neighbouring address block and
                # mistake that isolated label for this block's company name.
                sx1, _, sx2, _ = seg["bbox"]
                if not (left <= (sx1 + sx2) / 2 <= right):
                    continue
                selected=[]
                for word in seg["words"]:
                    x1,y1,x2,y2=word.bbox; c=(x1+x2)/2
                    if id(word) in label_ids or not (left <= c <= right): continue
                    if y1 > end_y: continue
                    # Vertical labels sit to the left of content on the same rows.
                    # A company name can be one row above such a sidebar.
                    if sidebar:
                        if y1 < ay1 - 2 * label_height or x1 <= ax2 + 3: continue
                    elif y1 <= ay1 + 6:
                        continue
                    selected.append(word)
                if not selected: continue
                text=clean_text(" ".join(word.text for word in selected)); low=_fold(text)
                if any(tok in low for tok in _ROLE_STOP_TOKENS):
                    continue
                if re.match(r"^en\s+\d+\s+exemplaire", low):
                    continue
                collected.append((min(w.bbox[1] for w in selected), min(w.bbox[0] for w in selected), max(w.bbox[2] for w in selected), text))
            # Rejoin postcode/city fragments split by a wide visual gap, while
            # leaving distant customer codes and the neighbouring column alone.
            grouped=[]
            for y, x1, x2, text in sorted(collected,key=lambda item:(item[0],item[1])):
                if grouped and abs(y-grouped[-1][0]) <= 4 and -2 <= x1-grouped[-1][2] <= 85:
                    grouped[-1]=(grouped[-1][0],grouped[-1][1],x2,grouped[-1][3]+" "+text)
                else:
                    grouped.append((y,x1,x2,text))
            lines=[]; seen=set()
            for _,_,_,text in grouped:
                k=_fold(text)
                if k and k not in seen and not re.fullmatch(r"\d+",k):
                    lines.append(text); seen.add(k)
            contacts, lines = _block_contacts(lines)
            party_name,department,address_lines=_choose_party_and_address(lines)
            if not address_lines: continue
            candidate=Address(raw_lines=address_lines,line1=address_lines[0] if address_lines else None,
                              line2=address_lines[1] if len(address_lines)>1 else None,
                              line3=address_lines[2] if len(address_lines)>2 else None)
            cand,cc,_=parse_address(candidate,infer_country=True)
            _,cand_score,_=local_validation(cand,cc)
            existing=getattr(result,role,None)
            if existing is None:
                continue
            cur,curc,_=parse_address(existing.address,infer_country=True)
            _,cur_score,_=local_validation(cur,curc)
            # An explicit labelled geometry block is stronger evidence than a mixed generic header parse.
            # Require postal locality plus a meaningful structural score so labels cannot absorb nearby tables.
            explicit_good = bool(cand.postal_code and cand.city and cand_score >= 0.72)
            if explicit_good or cand_score >= max(0.58,cur_score+0.05) or not existing.address.postal_code or not existing.address.city:
                changed_locality = bool(cur.postal_code and cand.postal_code and cur.postal_code != cand.postal_code)
                if changed_locality:
                    # Contact values inherited from the wrong former block must
                    # not survive a proven change of the role's postal locality.
                    existing.contact = Contact()
                    existing.phone = existing.fax = existing.email = None
                    existing.department = None
                existing.address=candidate
                if party_name:
                    existing.name=party_name
                    existing.legal_name=party_name
                if department:
                    existing.department=department
                for key,value in contacts.items():
                    setattr(existing.contact,key,value)
                    if key in {"phone","fax","email"}:
                        setattr(existing,key,value)
    return result

def build_business_addresses(result, pages: list[PageResult], config: dict | None = None) -> list[BusinessAddress]:
    result = recover_explicit_role_blocks(result, pages)
    cfg = (config or {}).get("addresses", {}) if isinstance(config, dict) else {}
    min_conf = float(cfg.get("min_address_confidence", 0.60))
    role_order = cfg.get("role_priority") or [
        "buyer", "supplier", "ship_to", "bill_to", "sold_to", "deliver_to",
        "invoice_to", "payer", "end_customer", "consignee", "ship_from",
    ]
    out: list[BusinessAddress] = []
    for role in role_order:
        party = getattr(result, role, None)
        if not _party_has_content(party) or not _nonempty_address(party.address):
            continue
        infer_country = bool(cfg.get("infer_country_from_postal_pattern", True))
        addr, component_confidence, parse_warnings = parse_address(party.address, infer_country=infer_country)
        validation_status, validation_score, validation_warnings = local_validation(addr, component_confidence)
        addr_conf = _address_confidence(addr, component_confidence)
        if addr_conf < min_conf:
            continue
        role_conf = float(EXPLICIT_ROLE_CONFIDENCE.get(role, 0.90))
        evidence = _find_evidence(pages, party, addr)
        conf = round(min(0.99, 0.55 * role_conf + 0.45 * addr_conf), 4)
        fp = address_fingerprint(addr)
        warnings: list[str] = list(dict.fromkeys(parse_warnings + validation_warnings))
        if not addr.postal_code and "postal_code_missing" not in warnings:
            warnings.append("postal_code_missing")
        if not addr.city and "city_missing" not in warnings:
            warnings.append("city_missing")

        # Optional authoritative verification against France's official BAN-backed geocoder.
        verify_cfg = cfg.get("verification", {}) if isinstance(cfg.get("verification", {}), dict) else {}
        if bool(verify_cfg.get("enabled", False)) and addr.country_code == "FR":
            vr = verify_french_address(
                addr, endpoint=str(verify_cfg.get("endpoint", "https://data.geopf.fr/geocodage/search")),
                timeout=float(verify_cfg.get("timeout_seconds", 3.5)),
            )
            addr.verification_status = vr.status
            addr.verification_provider = vr.provider
            addr.verification_score = vr.score
            addr.canonical_label = vr.label
            addr.latitude = vr.latitude
            addr.longitude = vr.longitude
            addr.verification_type = vr.result_type
            if vr.insee_code:
                addr.insee_code = vr.insee_code
            if vr.status == "VERIFIED":
                validation_status = "VERIFIED"
                validation_score = max(validation_score, float(vr.score or 0.0))
                addr_conf = max(addr_conf, min(0.995, 0.88 + 0.12 * float(vr.score or 0.0)))
            elif vr.status in {"AMBIGUOUS", "NOT_FOUND"}:
                warnings.append(f"official_address_{vr.status.lower()}")
                validation_status = vr.status
                addr_conf = min(addr_conf, 0.79 if vr.status == "AMBIGUOUS" else 0.69)
            elif vr.status == "UNAVAILABLE":
                warnings.append("official_address_verification_unavailable")
        elif addr.verification_status is None:
            addr.verification_status = "NOT_REQUESTED"
        conf = round(min(0.99, 0.55 * role_conf + 0.45 * addr_conf), 4)
        out.append(BusinessAddress(
            address_id=f"addr_{fp or len(out)+1}", role=role,
            role_label=ROLE_LABELS_FR.get(role, role), party_name=party.name,
            party_code=party.code, department=party.department,
            contact_name=party.contact.name, contact_email=party.contact.email or party.email,
            contact_phone=party.contact.phone or party.phone,
            address=addr, formatted_address=formatted_address(addr), address_fingerprint=fp,
            role_confidence=role_conf, address_confidence=addr_conf, confidence=conf,
            component_confidence=component_confidence, validation_status=validation_status, validation_score=validation_score,
            is_verified_real_address=(validation_status == "VERIFIED"),
            evidence=evidence, warnings=warnings,
        ))

    # Explicitly expose multiple business roles sharing one physical address rather
    # than deduplicating them away (e.g. buyer == ship-to on self-delivery orders).
    by_fp: dict[str, list[BusinessAddress]] = {}
    for item in out:
        if item.address_fingerprint:
            by_fp.setdefault(item.address_fingerprint, []).append(item)
    for group in by_fp.values():
        if len(group) > 1:
            roles = sorted({x.role for x in group})
            for item in group:
                item.shared_with_roles = [r for r in roles if r != item.role]
    return out
