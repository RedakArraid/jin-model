from __future__ import annotations
import re, unicodedata
from typing import Any

STREET_TYPES={'RUE','R','AVENUE','AV','BOULEVARD','BD','ROUTE','RTE','CHEMIN','CHE','CHEM','DEPARTEMENTALE','ALLEE','IMPASSE','PLACE','QUAI','COURS','PASSAGE','SQUARE','VOIE','RONDPOINT','MONTEE','TRAVERSE'}
ZONE_PREFIXES={'ZI','ZA','ZAC','ZAE','PARC','ZONE'}
BUSINESS_PARK_PREFIXES={'TECHNIPARC','TECHNOPARC','ECOPARC','ECOPARK'}
BUILDING_WORDS={'BATIMENT','BAT','IMMEUBLE','RESIDENCE','ENTREE','ESCALIER','LOT'}
DATE_RE=re.compile(r'^(?:0?[1-9]|[12]\d|3[01])[/\.\-](?:0?[1-9]|1[0-2])[/\.\-](?:20\d{2}|\d{2})$')
ORDER_CODE_RE=re.compile(r'^[A-Z0-9][A-Z0-9._/-]{5,}$',re.I)

def _deaccent(s:str)->str:return ''.join(c for c in unicodedata.normalize('NFKD',str(s)) if not unicodedata.combining(c))
def _norm(s:Any)->str:return re.sub(r'[^A-Z0-9]+','',_deaccent(str(s or '')).upper())
def _strict_postal(t):
    raw=t['text'].strip(); return bool(re.fullmatch(r'\d{5}',raw) and 1000<=int(raw)<=98999)
def _nb(box,w,h):return [max(0,min(1000,round(1000*box[0]/w))),max(0,min(1000,round(1000*box[1]/h))),max(0,min(1000,round(1000*box[2]/w))),max(0,min(1000,round(1000*box[3]/h)))]

def visual_lines(block_lines:list[list[dict[str,Any]]]):
    words=[t for line in block_lines for t in line]
    if not words:return []
    hs=sorted(max(.1,t['bbox'][3]-t['bbox'][1]) for t in words); med=hs[len(hs)//2]; tol=max(2.5,min(5.0,med*.48))
    rows=[]
    for t in sorted(words,key=lambda z:(((z['bbox'][1]+z['bbox'][3])/2),z['bbox'][0])):
        cy=(t['bbox'][1]+t['bbox'][3])/2; best=None; bd=None
        for i,r in enumerate(rows):
            d=abs(cy-r['cy'])
            if d<=tol and (bd is None or d<bd):best=i;bd=d
        if best is None:rows.append({'cy':cy,'tokens':[t]})
        else:
            r=rows[best];r['tokens'].append(t);r['cy']=sum((x['bbox'][1]+x['bbox'][3])/2 for x in r['tokens'])/len(r['tokens'])
    rows.sort(key=lambda r:r['cy']);out=[]
    for i,r in enumerate(rows):
        toks=sorted(r['tokens'],key=lambda t:t['bbox'][0]);out.append({'index':i,'cy':r['cy'],'tokens':toks,'text':' '.join(t['text'] for t in toks),'norms':[t.get('n') or _norm(t['text']) for t in toks]})
    return out

def anchored_fields(rows):
    out={}
    for row in rows:
        txt=_deaccent(row['text']).upper(); toks=row['tokens']; norms=row['norms']
        if 'COMMANDE' in txt and ('N°' in row['text'] or 'N DE COMMANDE' in txt or txt.strip().startswith('NO')):
            cs=[t for t in toks if _norm(t['text'])=='COMMANDE']
            if cs:
                ax=max(t['bbox'][2] for t in cs)
                for t in toks:
                    raw=t['text'].strip().strip(':;')
                    n=_norm(raw)
                    if t['bbox'][0]>=ax-2 and len(n)>=7 and ORDER_CODE_RE.fullmatch(raw) and any(c.isdigit() for c in n) and any(c.isalpha() for c in n):
                        out.setdefault('order_number',_field(raw,t['bbox'],t,.995));break
        if re.search(r'\bDATE\b',txt):
            ds=[t for t in toks if _norm(t['text'])=='DATE'];ax=max((t['bbox'][2] for t in ds),default=-1)
            for t in toks:
                if t['bbox'][0]>ax and DATE_RE.fullmatch(t['text'].strip()):out.setdefault('order_date',_field(t['text'].strip(),t['bbox'],t,.995));break
        if 'TOTAL' in norms and ('HT' in norms or 'HORS' in norms):
            ix=max(i for i,n in enumerate(norms) if n in {'HT','TOTAL'}); right=[t for t in toks if t['bbox'][0]>toks[ix]['bbox'][2]]
            groups=[];cur=[];last=None
            for t in right:
                raw=t['text'].strip();numeric=bool(re.fullmatch(r'[0-9][0-9\s\u00a0\u202f.,]*',raw))
                if numeric:
                    if cur and last is not None and t['bbox'][0]-last>8:groups.append(cur);cur=[]
                    cur.append(t);last=t['bbox'][2]
                elif cur:groups.append(cur);cur=[];last=None
            if cur:groups.append(cur)
            if groups:
                g=groups[-1];value=re.sub(r'[\s\u00a0\u202f.]','',' '.join(t['text'].strip() for t in g))
                if re.fullmatch(r'\d+(?:,\d{2})?',value):
                    box=[g[0]['bbox'][0],min(t['bbox'][1] for t in g),g[-1]['bbox'][2],max(t['bbox'][3] for t in g)];out.setdefault('total_net',_field(value,box,g[0],.995))
    return out

def _field(value,box,t,conf):return {'value':value,'confidence':conf,'source':'geometry_anchor_v2','page':1,'bbox':_nb(box,t['page_width'],t['page_height']),'requires_review':True}

def _street(row):
    toks=row['tokens'];ns=row['norms']
    for i,n in enumerate(ns):
        if n not in STREET_TYPES:continue
        house=suffix=None
        if i>0 and re.fullmatch(r'\d{1,4}(?:[-/]\d{1,4})?',toks[i-1]['text'].strip()):house=toks[i-1]
        elif i>1 and ns[i-1] in {'BIS','TER','QUATER'} and re.fullmatch(r'\d{1,4}',toks[i-2]['text'].strip()):house=toks[i-2];suffix=toks[i-1]
        name=[];prev=toks[i]
        for t in toks[i+1:]:
            if t['bbox'][0]-prev['bbox'][2]>30:break
            if _norm(t['text']) in {'FRANCE','TEL','TELEPHONE','FAX','EMAIL','MAIL','BP','CS','TSA','EUR'} or _strict_postal(t):break
            if _norm(t['text']):name.append(t);prev=t
        if name:
            used=([house] if house else [])+([suffix] if suffix else [])+[toks[i]]+name
            return {'house':house,'suffix':suffix,'street_type':toks[i],'name':name,'bbox':[min(t['bbox'][0] for t in used),min(t['bbox'][1] for t in used),max(t['bbox'][2] for t in used),max(t['bbox'][3] for t in used)],'row':row}
    return None

def _locality(row,anchor_x,neighbors):
    pcs=[t for t in row['tokens'] if _strict_postal(t) and abs(t['bbox'][0]-anchor_x)<=180]
    if not pcs:return None
    pc=min(pcs,key=lambda t:abs(t['bbox'][0]-anchor_x)); search=[t for t in row['tokens'] if t['bbox'][0]>pc['bbox'][2]-1]
    for nr in neighbors:
        if abs(nr['cy']-row['cy'])<=10:search += [t for t in nr['tokens'] if t['bbox'][0]>pc['bbox'][2]-1]
    search=sorted(search,key=lambda t:(abs(((t['bbox'][1]+t['bbox'][3])/2)-row['cy']),t['bbox'][0]));city=[];cedex=None;prev=pc
    for t in search:
        gap=t['bbox'][0]-prev['bbox'][2];n=_norm(t['text'])
        if n=='CEDEX':cedex=t;break
        if gap>100 and not city:continue
        if city and gap>35:break
        if n in {'FRANCE','EUR','TEL','FAX'}:break
        if re.fullmatch(r'\d+',t['text'].strip()):continue
        if any(c.isalpha() for c in t['text']):city.append(t);prev=t
    return {'postal':pc,'city':city,'cedex':cedex,'row':row} if city else None

def _row_segments(row, gap=60):
    toks=row['tokens']
    if not toks:return []
    groups=[];cur=[toks[0]]
    for t in toks[1:]:
        if t['bbox'][0]-cur[-1]['bbox'][2] > gap:
            groups.append(cur);cur=[t]
        else:cur.append(t)
    groups.append(cur)
    return [
        {'index':row['index'],'cy':row['cy'],'tokens':group,'text':' '.join(t['text'] for t in group),'norms':[t.get('n') or _norm(t['text']) for t in group]}
        for group in groups
    ]

def addresses(rows):
    anchors={}
    for r in rows:
        txt=_deaccent(r['text']).upper()
        if 'ADRESSE DE LIVRAISON' in txt:anchors['ship_to']={'y':r['cy']}
        if 'FACTURE A' in txt or 'FACTUREE A' in txt:anchors['bill_to']={'y':r['cy']}
    if not rows:return []
    pw=rows[0]['tokens'][0]['page_width'];out=[]
    street_rows=[segment for base_row in rows for segment in _row_segments(base_row)]
    for row in street_rows:
        s=_street(row)
        if not s:continue
        y=row['cy'];x=s['bbox'][0];role='unknown';ship=anchors.get('ship_to');bill=anchors.get('bill_to')
        if ship and y>ship['y'] and y-ship['y']<110 and x>pw*.40:role='ship_to'
        elif bill and y>bill['y'] and y-bill['y']<110 and x<pw*.40:role='bill_to'
        elif ship and y<ship['y'] and x>pw*.40 and ship['y']-y<120:role='supplier'
        loc=None;loc_idx=None
        for r2 in rows[row['index']+1:row['index']+7]:
            cand=_locality(r2,x,rows[r2['index']+1:r2['index']+3])
            if cand:loc=cand;loc_idx=r2['index'];break
        zone=building=None;extras=[];upper=max(0,row['index']-2);lower=(loc_idx+1 if loc_idx is not None else row['index']+5)
        for r2 in rows[upper:lower]:
            if r2['index']==row['index']:continue
            col=[t for t in r2['tokens'] if abs(t['bbox'][0]-x)<=170 and ((x>pw*.4 and t['bbox'][0]>pw*.4) or (x<=pw*.4 and t['bbox'][0]<pw*.4))]
            if not col:continue
            ns=[_norm(t['text']) for t in col];txt=' '.join(t['text'] for t in col)
            if ns and ns[0] in ZONE_PREFIXES:zone=txt
            if any(n in BUILDING_WORDS for n in ns):building=txt
            for k,n in enumerate(ns[:-1]):
                if n in {'BP','CS','TSA'} and re.fullmatch(r'\d{3,6}',col[k+1]['text'].strip()):extras.append((n.lower(),f"{col[k]['text']} {col[k+1]['text']}"))
        c={'house_number':s['house']['text'] if s['house'] else None,'house_number_suffix':s['suffix']['text'] if s['suffix'] else None,'street_type':s['street_type']['text'],'street_name':' '.join(t['text'] for t in s['name']),'industrial_zone':zone,'building':building,'postal_code':loc['postal']['text'] if loc else None,'city':' '.join(t['text'] for t in loc['city']) if loc else None,'cedex':bool(loc and loc['cedex'])}
        for k,v in extras:c[k]=v
        c={k:v for k,v in c.items() if v not in (None,False,'')};street=' '.join(str(v) for v in [c.get('house_number'),c.get('house_number_suffix'),c.get('street_type'),c.get('street_name')] if v);parts=[]
        if c.get('building'):parts.append(c['building'])
        if street:parts.append(street)
        if c.get('industrial_zone'):parts.append(c['industrial_zone'])
        for k in ('bp','cs','tsa'):
            if c.get(k):parts.append(c[k])
        local=' '.join(str(v) for v in [c.get('postal_code'),c.get('city'),('CEDEX' if c.get('cedex') else None)] if v)
        if local:parts.append(local)
        out.append({'role':role,'components':c,'formatted_address_suggestion':', '.join(parts),'confidence':.995 if loc else .97,'source':'geometry_address_v2','requires_review':True,'source_line':row['index']})
    seen=set();uniq=[]
    for a in out:
        key=(a['role'],_norm(a['formatted_address_suggestion']))
        if key not in seen:seen.add(key);uniq.append(a)
    return uniq

def extract_geometry_suggestions(block_lines):
    rows=visual_lines(block_lines)
    return {'anchored_fields':anchored_fields(rows),'address_candidates':addresses(rows),'geometry_version':'visual-lines-v2'}


# ---- V5.5.2 generalized visual-line extraction ---------------------------------
ROLE_PHRASES_V3 = {
    "ship_to": [
        "ADRESSE DE LIVRAISON",
        "ADRESSE LIVRAISON",
        "ADRESSE DESTINATAIRE",
        "A LIVRER A",
        "A LIVRER",
        "LIVRAISON SOUHAITEE A",
        "DEPOT A LIVRER",
        "LIEU DE LIVRAISON",
    ],
    "bill_to": ["ADRESSE DE FACTURATION", "A FACTURER A", "FACTURE A", "FACTUREE A"],
}


def _xcenter_v3(token):
    return (token["bbox"][0] + token["bbox"][2]) / 2


def _postal_value_v3(token):
    raw = token["text"].strip().upper()
    if re.fullmatch(r"\d{5}", raw) and 1000 <= int(raw) <= 98999:
        return raw
    match = re.fullmatch(r"(?:F|FR)[- ]?(\d{5})", raw)
    if match and 1000 <= int(match.group(1)) <= 98999:
        return match.group(1)
    attached = re.fullmatch(r"(\d{5})([A-Z][A-Z\-']+)", _deaccent(raw))
    if attached and 1000 <= int(attached.group(1)) <= 98999:
        return attached.group(1)
    return None


def _postal_tokens_v3(row):
    """Return native and safely reconstructed French postal-code tokens.

    Several order templates print a visual space inside the postal code
    (``03 100``, ``64 310``). PDF text extraction consequently exposes two
    tokens although the value is one five-digit postcode. Only the common
    two-plus-three form is reconstructed, with a tight horizontal-gap check,
    to avoid turning arbitrary table amounts into localities.
    """
    tokens = row["tokens"]
    found = []
    for token in tokens:
        postal = _postal_value_v3(token)
        if not postal:
            continue
        attached = re.fullmatch(
            r"\d{5}([A-Z][A-Z\-']+)", _deaccent(token["text"].strip().upper())
        )
        if attached:
            reconstructed = dict(token)
            reconstructed["text"] = postal
            reconstructed["attached_city"] = attached.group(1)
            found.append(reconstructed)
        else:
            found.append(token)
    for index, left in enumerate(tokens[:-1]):
        right = tokens[index + 1]
        left_text = left["text"].strip()
        right_text = right["text"].strip()
        if not (re.fullmatch(r"\d{2}", left_text) and re.fullmatch(r"\d{3}", right_text)):
            continue
        combined = left_text + right_text
        if not (1000 <= int(combined) <= 98999):
            continue
        gap = right["bbox"][0] - left["bbox"][2]
        height = max(left["bbox"][3] - left["bbox"][1], right["bbox"][3] - right["bbox"][1])
        if gap < -1 or gap > max(12.0, height * 1.5):
            continue
        synthetic = dict(left)
        synthetic["text"] = combined
        synthetic["bbox"] = [
            left["bbox"][0],
            min(left["bbox"][1], right["bbox"][1]),
            right["bbox"][2],
            max(left["bbox"][3], right["bbox"][3]),
        ]
        synthetic["reconstructed_from"] = [left_text, right_text]
        found.append(synthetic)
    for index, left in enumerate(tokens[:-1]):
        right = tokens[index + 1]
        left_text = left["text"].strip()
        right_text = right["text"].strip()
        attached = re.fullmatch(
            r"(\d)([A-Z][A-Z'-]+)", _deaccent(right_text).upper()
        )
        if not (re.fullmatch(r"\d{4}", left_text) and attached):
            continue
        combined = left_text + attached.group(1)
        if not (1000 <= int(combined) <= 98999):
            continue
        gap = right["bbox"][0] - left["bbox"][2]
        height = max(
            left["bbox"][3] - left["bbox"][1],
            right["bbox"][3] - right["bbox"][1],
        )
        if gap < -1 or gap > max(12.0, height * 1.5):
            continue
        synthetic = dict(left)
        synthetic["text"] = combined
        synthetic["bbox"] = [
            left["bbox"][0],
            min(left["bbox"][1], right["bbox"][1]),
            right["bbox"][2],
            max(left["bbox"][3], right["bbox"][3]),
        ]
        synthetic["reconstructed_from"] = [left_text, right_text]
        synthetic["attached_city"] = attached.group(2)
        found.append(synthetic)
    return found


def _explicit_order_number_v3(rows):
    """Read the value attached to an explicit COMMANDE label.

    The core extractor is intentionally broad and can confuse a nearby
    ``N/REF`` with the customer's purchase-order number. This geometry rule is
    deliberately narrow: it accepts only the same visual row or the immediately
    following row under an explicit COMMANDE label, and stops before another
    semantic header such as an address or a date.
    """
    stop_words = {
        "ADRESSE", "LIVRAISON", "DATE", "FOURNISSEUR", "CLIENT", "CONTACT",
        "FACTURATION", "ACHETEUR", "REFERENCE", "REF", "PAGE", "TEL", "FAX",
    }
    skip_words = {"N", "NO", "NUMERO", "DE", "DU", "FOURNISSEUR", "CLIENT"}

    attached_number_pattern = (
        r"N\s*(?:[\u00b0\u00ba]|O|UMERO)?\s*[:#.-]?\s*"
        r"([A-Z0-9][A-Z0-9._/\-]{2,63})"
    )

    def collect(tokens, start, label_x, allow_leading_no=False):
        selected = []
        previous = None
        remaining = tokens[start:]
        selected_end = None
        for offset, token in enumerate(remaining):
            raw = token["text"].strip().strip(":;")
            norm = _norm(raw)
            if not norm:
                continue
            attached_number = re.fullmatch(
                attached_number_pattern,
                _deaccent(raw),
                flags=re.I,
            )
            if not selected and attached_number:
                raw = attached_number.group(1)
                norm = _norm(raw)
            if not selected and norm in skip_words:
                if allow_leading_no and norm in {"NO", "N"}:
                    selected.append(token)
                continue
            if norm in stop_words:
                break
            if previous is not None and token["bbox"][0] - previous["bbox"][2] > 35:
                break
            if token["bbox"][0] < label_x - 20:
                continue
            if not re.fullmatch(r"[A-Z0-9._/\-]+", raw, flags=re.I):
                if selected:
                    break
                continue
            selected.append(token)
            previous = token
            selected_end = offset
            compact_selected = _norm(" ".join(item["text"] for item in selected))
            selected_digits = sum(char.isdigit() for char in compact_selected)
            # Stop as soon as a complete identifier has been assembled. This
            # avoids swallowing a duplicated number, a sequence counter or the
            # order date printed later on the same visual row.
            if len(compact_selected) >= 5 and selected_digits >= 4:
                next_token = remaining[offset + 1] if offset + 1 < len(remaining) else None
                next_raw = next_token["text"].strip().strip(":;") if next_token else ""
                tight_suffix = bool(
                    next_token
                    and next_token["bbox"][0] - token["bbox"][2] <= 35
                    and re.fullmatch(r"[/._-]\d{2,}", next_raw)
                )
                if tight_suffix:
                    continue
                break
            if len(selected) >= 6:
                break
        if not selected:
            return None
        value = " ".join(token["text"].strip().strip(":;") for token in selected)
        first_raw = selected[0]["text"].strip().strip(":;")
        first_attached = re.fullmatch(
            r"N\s*(?:[\u00b0\u00ba]|O|UMERO)?\s*[:#.-]?\s*"
            r"([A-Z0-9][A-Z0-9._/\-]{2,63})",
            _deaccent(first_raw),
            flags=re.I,
        )
        if first_attached:
            value = " ".join(
                [first_attached.group(1)]
                + [token["text"].strip().strip(":;") for token in selected[1:]]
            )
        next_token = (
            remaining[selected_end + 1]
            if selected_end is not None and selected_end + 1 < len(remaining)
            else None
        )
        if (
            re.fullmatch(r"[\d\s./\-]+", value)
            and next_token
            and _norm(next_token["text"]) in STREET_TYPES
        ):
            # A title in the left column can be horizontally aligned with a
            # supplier address in the right column (for example
            # ``COMMANDE     124 126 AV STALINGRAD``). Street numbers are not
            # customer purchase-order identifiers.
            return None
        if DATE_RE.fullmatch(value):
            return None
        compact = _norm(value)
        digits = sum(char.isdigit() for char in compact)
        if len(compact) < 5 or digits < 4:
            return None
        return _field(value, _union_bbox_v3(selected), selected[0], 0.998) | {
            "source": "geometry_explicit_order_label_v3",
        }

    def collect_reference_column(label_row, reference, command):
        """Read the complete cell below an exact ``Référence Commande`` header."""
        for following in rows[label_row["index"] + 1 : label_row["index"] + 3]:
            if following["cy"] - label_row["cy"] > 45:
                break
            selected = []
            previous = None
            for token in following["tokens"]:
                if token["bbox"][0] < reference["bbox"][0] - 10:
                    continue
                if token["bbox"][0] > command["bbox"][2] + 45:
                    break
                raw = token["text"].strip().strip(":;")
                if not re.fullmatch(r"[A-Z0-9._/\-]+", raw, flags=re.I):
                    if selected:
                        break
                    continue
                if previous is not None and token["bbox"][0] - previous["bbox"][2] > 25:
                    break
                selected.append(token)
                previous = token
            if not selected:
                continue
            while selected and re.fullmatch(
                r"[./_\-]+", selected[-1]["text"].strip()
            ):
                selected.pop()
            if not selected:
                continue
            value = " ".join(token["text"].strip().strip(":;") for token in selected)
            compact = _norm(value)
            if len(compact) < 5 or sum(char.isdigit() for char in compact) < 4:
                continue
            return _field(value, _union_bbox_v3(selected), selected[0], 0.998) | {
                "source": "geometry_explicit_order_label_v3",
                "source_text": value,
            }
        return None

    # Legacy ERP header: ``N° CF 15 4 000572686`` with DATE/PAGE printed in
    # neighboring visual cells.  OCR often misses the large document title,
    # so use the explicit N° marker, a known order-series prefix, one to three
    # short routing groups and a final long number instead.
    upper_limit = max(1, int(len(rows) * 0.35))
    for row in rows[:upper_limit]:
        tokens = row["tokens"]
        norms = [_norm(token["text"]) for token in tokens]
        marker_positions = [
            index for index, norm in enumerate(norms)
            if norm in {"N", "NO", "NUMERO"}
        ]
        for marker in marker_positions:
            tail = [
                (index, norms[index])
                for index in range(marker + 1, min(len(tokens), marker + 7))
                if norms[index]
            ]
            if len(tail) < 4 or tail[0][1] not in {"CF", "CDE", "CMD", "CM", "PO"}:
                continue
            values = [value for _, value in tail]
            final_positions = [
                pos for pos, value in enumerate(values[1:], start=1)
                if re.fullmatch(r"\d{6,12}", value)
            ]
            if not final_positions:
                continue
            final_pos = final_positions[0]
            routing = values[1:final_pos]
            if not (0 <= len(routing) <= 3) or not all(
                re.fullmatch(r"\d{1,3}", value) for value in routing
            ):
                continue
            nearby_date_context = any(
                abs(float(candidate["cy"]) - float(row["cy"])) <= 18
                and (
                    "DATE" in candidate["norms"]
                    or any(DATE_RE.fullmatch(token["text"].strip()) for token in candidate["tokens"])
                )
                for candidate in rows
            )
            if not nearby_date_context:
                continue
            selected_indexes = [tail[pos][0] for pos in range(final_pos + 1)]
            selected = [tokens[index] for index in selected_indexes]
            value = values[0] + values[final_pos]
            candidate = _field(value, _union_bbox_v3(selected), selected[0], 0.999)
            candidate["source"] = "geometry_explicit_order_label_v3"
            candidate["source_text"] = " ".join(token["text"].strip() for token in selected)
            return candidate

    for row in rows:
        command_indexes = [
            index for index, norm in enumerate(row["norms"])
            if norm == "COMMANDE"
        ]
        if not command_indexes:
            continue
        for command_index in command_indexes:
            command = row["tokens"][command_index]
            nearby = row["norms"][max(0, command_index - 2) : command_index + 3]
            has_number_marker = any(norm in {"N", "NO", "NUMERO"} for norm in nearby)
            previous_norms = row["norms"][max(0, command_index - 2) : command_index]
            next_norms = row["norms"][command_index + 1 : command_index + 4]
            if "CLIENT" in next_norms and any(
                norm in {"LA", "UNE", "CETTE", "VOTRE", "NOTRE", "POUR"}
                for norm in previous_norms
            ):
                # A product note such as ``pour la commande client n° ...``
                # carries a downstream reference, not this document's PO ID.
                continue
            if command_index >= 2 and row["norms"][command_index - 1] == "DE" and row["norms"][command_index - 2] in {
                "BOITIER", "CARTE", "COFFRET", "MODULE", "PANNEAU", "TABLEAU",
            }:
                # Product descriptions such as ``TABLEAU DE COMMANDE`` are not
                # order-number labels. Without this guard, the first quantity
                # and article reference to their right can replace a valid PO.
                continue
            if command_index > 0 and row["norms"][command_index - 1] == "REFERENCE":
                reference_value = collect_reference_column(
                    row, row["tokens"][command_index - 1], command
                )
                if reference_value:
                    return reference_value
            following_norms = row["norms"][command_index + 1 : command_index + 3]
            if following_norms[:2] == ["A", "DISTANCE"] or following_norms[:1] == ["DISTANCE"]:
                continue
            preceded_by_non_identifier_label = command_index > 0 and row["norms"][command_index - 1] in {
                "DATE", "LIVRAISON", "MODE", "CONDITIONS", "SUR",
            }
            if preceded_by_non_identifier_label:
                continue
            same_row = collect(
                row["tokens"], command_index + 1, command["bbox"][0]
            )
            if same_row:
                same_row["source_text"] = same_row["value"]
                return same_row
            following_rows = rows[row["index"] + 1 : row["index"] + 3]
            has_attached_below = any(
                re.fullmatch(
                    attached_number_pattern,
                    _deaccent(token["text"].strip().strip(":;")),
                    flags=re.I,
                )
                for following in following_rows
                if following["cy"] - row["cy"] <= 35
                for token in following["tokens"]
                if token["bbox"][0] >= command["bbox"][0] - 20
            )
            if not has_number_marker and not has_attached_below:
                continue
            for following in following_rows:
                if following["cy"] - row["cy"] > 35:
                    break
                below = collect(
                    following["tokens"], 0, command["bbox"][0], allow_leading_no=True
                )
                if below:
                    below["source_text"] = below["value"]
                    return below

    # A few purchasing systems title the document COMMANDE but label the
    # customer identifier "N° dossier". Accept that label only when a command
    # title exists elsewhere on the same page, so ordinary case-file numbers
    # in unrelated documents are never promoted.
    has_order_title = any("COMMANDE" in row["norms"] for row in rows)
    if has_order_title:
        for row in rows:
            for dossier_index, norm in enumerate(row["norms"]):
                if norm != "DOSSIER":
                    continue
                candidate = collect(
                    row["tokens"], dossier_index + 1,
                    row["tokens"][dossier_index]["bbox"][0],
                )
                if candidate:
                    candidate["source_text"] = candidate["value"]
                    return candidate
        # Compact ERP rows often expose a customer number such as
        # ``CF 000665669`` next to the order date, without repeating a label.
        # Restrict the fallback to well-known PO prefixes near the page top and
        # require an adjacent date, which excludes article and supplier codes.
        for row in rows[: max(1, int(len(rows) * 0.35))]:
            has_date = any(DATE_RE.fullmatch(token["text"].strip()) for token in row["tokens"])
            if not has_date:
                continue
            for index, token in enumerate(row["tokens"][:-1]):
                if _norm(token["text"]) not in {"CF", "CDE", "CMD", "CM", "PO"}:
                    continue
                number = row["tokens"][index + 1]
                if not re.fullmatch(r"\d{5,12}", number["text"].strip()):
                    continue
                candidate = _field(
                    f"{token['text'].strip()}{number['text'].strip()}",
                    _union_bbox_v3([token, number]),
                    token,
                    0.995,
                )
                candidate["source"] = "geometry_explicit_order_label_v3"
                candidate["source_text"] = (
                    f"{token['text'].strip()} {number['text'].strip()}"
                )
                return candidate
    return None


def _union_bbox_v3(tokens):
    return [
        min(token["bbox"][0] for token in tokens),
        min(token["bbox"][1] for token in tokens),
        max(token["bbox"][2] for token in tokens),
        max(token["bbox"][3] for token in tokens),
    ]


def _phrase_anchor_v3(row, phrases):
    text = _deaccent(row["text"]).upper()
    for phrase in phrases:
        if phrase not in text:
            continue
        words = phrase.split()
        norms = [_norm(token["text"]) for token in row["tokens"]]
        target = [_norm(word) for word in words]
        for start in range(len(norms)):
            pos = start
            matched = []
            for wanted in target:
                while pos < len(norms) and not norms[pos]:
                    pos += 1
                if pos >= len(norms) or norms[pos] != wanted:
                    break
                matched.append(row["tokens"][pos])
                pos += 1
            if len(matched) == len(target):
                # Short labels beginning with ``A`` (``A LIVRER``,
                # ``A FACTURER A``) are meaningful as headings, but the
                # exact same words also occur naturally inside comments such
                # as "urgent svp, a livrer au plus vite".  A role anchor must
                # therefore start the visual line (or follow the explicit
                # heading word DEPOT), otherwise it can incorrectly extend a
                # delivery column over a neighbouring supplier address.
                meaningful_before = [value for value in norms[:start] if value]
                if target and target[0] == "A":
                    starts_heading = meaningful_before in ([], ["DEPOT"])
                    follows_parallel_role_heading = any(
                        value in {"FACTURER", "FACTURATION", "LIVRAISON"}
                        for value in meaningful_before
                    )
                    previous_token = row["tokens"][start - 1] if start else None
                    page_width = float(matched[0].get("page_width") or 0.0)
                    starts_visual_column = bool(
                        previous_token
                        and page_width > 0
                        and matched[0]["bbox"][0] - previous_token["bbox"][2]
                        >= page_width * 0.08
                    )
                    if not (
                        starts_heading
                        or follows_parallel_role_heading
                        or starts_visual_column
                    ):
                        continue
                return {
                    "x": matched[0]["bbox"][0],
                    "y": row["cy"],
                    "bbox": [
                        matched[0]["bbox"][0],
                        min(token["bbox"][1] for token in matched),
                        matched[-1]["bbox"][2],
                        max(token["bbox"][3] for token in matched),
                    ],
                }
        first = _norm(words[0])
        # Never approximate a generic ``A ...`` heading from its first token
        # alone.  For example, ``A LIVRER A`` is a raw substring of
        # ``A LIVRER AU`` even though the token sequence does not match.
        if first == "A":
            continue
        candidates = [token for token in row["tokens"] if _norm(token["text"]) == first]
        if candidates:
            token = candidates[0]
            return {"x": token["bbox"][0], "y": row["cy"], "bbox": token["bbox"]}
    return None


def _numeric_value_v3(tokens):
    if not tokens:
        return None
    raw = "".join(token["text"].strip() for token in tokens)
    raw = raw.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    raw = raw.replace("€", "").replace("EUR", "")
    if re.fullmatch(r"[-+]?\d{1,3}(?:[.]\d{3})*(?:[,.]\d{2})?|[-+]?\d+(?:[,.]\d{2})?", raw):
        return raw
    return None


def _nearest_numeric_below_v3(rows, header_row, x, max_rows=3):
    best = None
    for row in rows[header_row["index"] + 1 : header_row["index"] + 1 + max_rows]:
        groups = []
        current = []
        last_x = None
        for token in row["tokens"]:
            text = token["text"].strip()
            numeric = bool(re.fullmatch(r"[0-9][0-9\s\u00a0\u202f.,]*", text))
            if numeric:
                if current and last_x is not None and token["bbox"][0] - last_x > 10:
                    groups.append(current)
                    current = []
                current.append(token)
                last_x = token["bbox"][2]
            elif current:
                groups.append(current)
                current = []
                last_x = None
        if current:
            groups.append(current)
        for group in groups:
            value = _numeric_value_v3(group)
            if not value:
                continue
            box = _union_bbox_v3(group)
            distance = abs((box[0] + box[2]) / 2 - x)
            if distance < 110 and (best is None or distance < best[0]):
                best = (distance, value, box, group[0])
    return best


def _anchored_fields_v3(rows):
    out = anchored_fields(rows)

    explicit_order_number = _explicit_order_number_v3(rows)
    if explicit_order_number:
        # An explicit visual COMMANDE label is stronger than the legacy
        # single-token heuristic, especially for numeric and spaced values.
        out["order_number"] = explicit_order_number

    # Explicit offer metadata. Do not expose an offer date as an order date.
    for row in rows:
        text = _deaccent(row["text"]).upper()
        norms = row["norms"]
        if "OFFRE N" in text or (
            "OFFRE" in norms
            and any("SCA" in _norm(token["text"]) for token in row["tokens"])
        ):
            for token in row["tokens"]:
                raw = token["text"].strip().strip(":;")
                if (
                    re.fullmatch(r"[A-Z]{2,5}-?[A-Z0-9-]{6,}", raw, re.I)
                    and any(char.isdigit() for char in raw)
                ):
                    out.setdefault("offer_number", _field(raw, token["bbox"], token, 0.995))
                    break
        if "DATE DE L OFFRE" in text or "DATE DE L'OFFRE" in row["text"].upper():
            for token in row["tokens"]:
                if DATE_RE.fullmatch(token["text"].strip()):
                    out.setdefault("offer_date", _field(token["text"].strip(), token["bbox"], token, 0.995))
                    break

    if "offer_number" in out and not any(
        "COMMANDE" in _deaccent(row["text"]).upper() for row in rows
    ):
        out.pop("order_date", None)

    # Header/value tables: N° Document, Pièce, N° Commande, Date.
    for row in rows:
        norms = row["norms"]
        tokens = row["tokens"]
        has_order_context = (
            "COMMANDE" in norms
            or any(
                "COMMANDE" in _deaccent(candidate["text"]).upper()
                for candidate in rows[max(0, row["index"] - 2) : row["index"] + 1]
            )
        )
        headers = []
        date_indexes = [index for index, norm in enumerate(norms) if norm == "DATE"]
        first_date_index = date_indexes[0] if date_indexes else None
        for index, norm in enumerate(norms):
            if norm in {"DOCUMENT", "PIECE"}:
                headers.append(("order_number", tokens[index]))
            elif norm == "COMMANDE" and "DATE" in norms and "CLIENT" in norms:
                headers.append(("order_number", tokens[index]))
            elif (
                norm in {"N", "NO", "NUMERO"}
                and first_date_index is not None
                and index < first_date_index
                and has_order_context
            ):
                # Common purchasing form: ``Numero | Date | Votre Reference``.
                # Only the number heading left of Date is the document ID; a
                # later ``N intracom. Client`` is a tax/customer field.
                headers.append(("order_number", tokens[index]))
            elif norm == "DATE":
                headers.append(("order_date", tokens[index]))
        header_is_order_table = has_order_context or (
            "DATE" in norms
            and any(norm in {"DOCUMENT", "PIECE"} for norm in norms)
        )
        if headers and header_is_order_table:
            following = rows[row["index"] + 1 : row["index"] + 3]
            for key, header in headers:
                target_x = _xcenter_v3(header)
                best = None
                for candidate_row in following:
                    for token in candidate_row["tokens"]:
                        distance = abs(_xcenter_v3(token) - target_x)
                        raw = token["text"].strip().strip(":;")
                        if key == "order_number" and first_date_index is not None:
                            joined_date = re.fullmatch(
                                r"(.{5,}?)(\d{2}[/.-]\d{2}[/.-]\d{2,4})",
                                raw,
                            )
                            if joined_date:
                                raw = joined_date.group(1)
                        valid = (
                            bool(DATE_RE.fullmatch(raw))
                            if key == "order_date"
                            else len(_norm(raw)) >= 5
                            and any(char.isdigit() for char in _norm(raw))
                        )
                        if valid and distance < 70 and (best is None or distance < best[0]):
                            best = (distance, token, raw)
                if best:
                    candidate = _field(best[2], best[1]["bbox"], best[1], 0.995)
                    if key == "order_number" and has_order_context:
                        # This is the value cell below an explicit order
                        # metadata heading (for example ``N° Document`` under
                        # ``Commande Fournisseur``), not a generic identifier.
                        candidate["source"] = "geometry_explicit_order_metadata_v3"
                        candidate["source_text"] = best[2]
                    out.setdefault(key, candidate)

    # Summary tables: NET H.T., TOTAL H.T., VALEUR/MONTANT TVA, TOTAL/MONTANT TTC, NET A PAYER.
    total_candidates = {}
    for row in rows:
        norms = row["norms"]
        tokens = row["tokens"]
        mappings = []
        for index, norm in enumerate(norms):
            if norm == "NET" and index + 1 < len(norms) and norms[index + 1] == "HT":
                mappings.append(("total_net", (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2, 3))
            # Some PDF text layers join adjacent summary headers (TTCNET),
            # or clip TOTAL TTC to TOTAL T at the right page edge. Accept
            # the latter only inside an explicit HT/TVA summary table.
            clipped_gross = (
                norm == "TOTAL"
                and index + 1 < len(norms)
                and norms[index + 1] == "T"
                and "TVA" in norms
                and "HT" in norms
                and tokens[index]["bbox"][0]
                > float(tokens[index].get("page_width") or 0) * 0.75
            )
            if norm == "TOTAL" and index + 1 < len(norms) and (
                norms[index + 1] in {"HT", "TTC", "TTCNET"} or clipped_gross
            ):
                mappings.append((
                    "total_net" if norms[index + 1] == "HT" else "total_gross",
                    (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2,
                    3,
                ))
            if norm in {"MONTANT", "VALEUR"} and index + 1 < len(norms) and norms[index + 1] == "TVA":
                mappings.append(("total_vat", (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2, 3))
            if norm == "MONTANT" and index + 1 < len(norms) and norms[index + 1] in {"TTC", "TTCNET"}:
                mappings.append(("total_gross", (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2, 3))
            if norm == "NET" and index + 1 < len(norms) and norms[index + 1] == "A":
                has_payer = index + 2 < len(norms) and norms[index + 2] == "PAYER"
                near_right_edge = (
                    float(tokens[index].get("page_width") or 0) > 0
                    and tokens[index]["bbox"][0]
                    > float(tokens[index]["page_width"]) * 0.82
                )
                if has_payer or near_right_edge:
                    end_token = tokens[index + 2] if has_payer else tokens[index + 1]
                    mappings.append((
                        "amount_due",
                        (tokens[index]["bbox"][0] + end_token["bbox"][2]) / 2,
                        3,
                    ))
        for key, x, depth in mappings:
            candidate = _nearest_numeric_below_v3(rows, row, x, depth)
            if candidate:
                score = (row["cy"], -candidate[0])
                if key not in total_candidates or score > total_candidates[key][0]:
                    total_candidates[key] = (score, candidate)
    for key, (_, candidate) in total_candidates.items():
        _, value, box, token = candidate
        out.setdefault(key, _field(value, box, token, 0.995))

    # A product row also contains several amounts (unit price, line total).
    # Without an explicit summary anchor it is not evidence of a gross total.
    # Inventing a total here also truncates the downstream line-items region.
    if "amount_due" not in out and "total_gross" in out:
        for row in rows:
            right_edge_net = []
            for index, token in enumerate(row["tokens"]):
                token_norm = _norm(token["text"])
                next_norm = (
                    _norm(row["tokens"][index + 1]["text"])
                    if index + 1 < len(row["tokens"])
                    else ""
                )
                page_width = float(token.get("page_width") or 0)
                if (
                    page_width > 0
                    and token["bbox"][0] > page_width * 0.75
                    and (token_norm == "NET" or token_norm.endswith("NET"))
                    and next_norm == "A"
                ):
                    right_edge_net.append(token)
            if right_edge_net:
                amount = dict(out["total_gross"])
                amount["confidence"] = min(
                    float(amount.get("confidence") or 0.0),
                    0.97,
                )
                amount["source"] = "geometry_summary_right_edge_fallback"
                out["amount_due"] = amount
                break

    return out


def _house_before_v3(tokens, index):
    # Prefer explicit ranges before a single previous number.
    if (
        index > 2
        and re.fullmatch(r"\d{1,4}", tokens[index - 3]["text"].strip().rstrip(","))
        and _norm(tokens[index - 2]["text"]) == "ET"
        and re.fullmatch(r"\d{1,4}", tokens[index - 1]["text"].strip().rstrip(","))
    ):
        left = tokens[index - 3]["text"].strip().rstrip(",")
        right = tokens[index - 1]["text"].strip().rstrip(",")
        return f"{left} et {right}", None, tokens[index - 3 : index]
    if (
        index > 2
        and re.fullmatch(r"\d{1,4}", tokens[index - 3]["text"].strip())
        and tokens[index - 2]["text"].strip() in {"-", "/"}
        and re.fullmatch(r"\d{1,4}", tokens[index - 1]["text"].strip())
    ):
        return (
            f"{tokens[index - 3]['text'].strip()}-{tokens[index - 1]['text'].strip()}",
            None,
            [tokens[index - 3], tokens[index - 2], tokens[index - 1]],
        )
    if (
        index > 1
        and re.fullmatch(r"\d{1,4}[,]?", tokens[index - 2]["text"].strip())
        and re.fullmatch(r"\d{1,4}", tokens[index - 1]["text"].strip())
    ):
        left = tokens[index - 2]["text"].strip().rstrip(",")
        return (
            f"{left}-{tokens[index - 1]['text'].strip()}",
            None,
            [tokens[index - 2], tokens[index - 1]],
        )
    if index > 0:
        raw = tokens[index - 1]["text"].strip()
        if re.fullmatch(r"\d{1,4}(?:\s*[,\-/]\s*\d{1,4})?,?", raw):
            normalized = raw.rstrip(",").replace(",", "-").replace("/", "-").replace(" ", "")
            return normalized, None, [tokens[index - 1]]
    if (
        index > 1
        and (
            _norm(tokens[index - 1]["text"]) in {"BIS", "TER", "QUATER", "A", "B", "C"}
            or tokens[index - 1]["text"].strip() in {"½", "Â", "1/2"}
        )
        and re.fullmatch(r"\d{1,4}", tokens[index - 2]["text"].strip())
    ):
        suffix = tokens[index - 1]["text"].strip()
        if suffix in {"Â", "1/2"}:
            suffix = "½"
        return (
            tokens[index - 2]["text"].strip(),
            suffix,
            [tokens[index - 2], tokens[index - 1]],
        )
    return None, None, []


def _street_v3(row):
    tokens = row["tokens"]
    norms = row["norms"]
    for index, norm in enumerate(norms):
        if norm not in STREET_TYPES:
            continue
        house_value, suffix_value, house_tokens = _house_before_v3(tokens, index)
        name = []
        previous = tokens[index]
        for token in tokens[index + 1 :]:
            if token["bbox"][0] - previous["bbox"][2] > 30:
                break
            if (
                _norm(token["text"])
                in {"FRANCE", "TEL", "TELEPHONE", "FAX", "EMAIL", "MAIL", "BP", "CS", "TSA", "EUR"}
                or _norm(token["text"]) in ZONE_PREFIXES | BUSINESS_PARK_PREFIXES
                or _strict_postal(token)
            ):
                break
            if _norm(token["text"]):
                name.append(token)
                previous = token
        if name:
            used = house_tokens + [tokens[index]] + name
            return {
                "house_value": house_value,
                "suffix_value": suffix_value,
                "street_type": tokens[index],
                "name": name,
                "bbox": _union_bbox_v3(used),
                "row": row,
            }
    return None


def _role_anchors_v3(rows):
    out = {}
    for row in rows:
        for role, phrases in ROLE_PHRASES_V3.items():
            anchor = _phrase_anchor_v3(row, phrases)
            if anchor:
                out.setdefault(role, []).append(anchor)
    if "ship_to" not in out and rows:
        page_width = rows[0]["tokens"][0]["page_width"]
        for row in rows:
            for index, token in enumerate(row["tokens"]):
                if (
                    _norm(token["text"]) == "LIVRAISON"
                    and token["bbox"][0] > page_width * 0.45
                    and any(
                        _norm(previous["text"]) == "DE"
                        for previous in row["tokens"][max(0, index - 3) : index]
                    )
                ):
                    out["ship_to"] = [{
                        "x": max(page_width * 0.48, token["bbox"][0] - 80),
                        "y": row["cy"],
                        "bbox": token["bbox"],
                        "fuzzy": True,
                    }]
                    return out
    return out


def _anchor_bands_v3(anchors, page_width):
    flat = []
    for role, items in anchors.items():
        for anchor in items:
            flat.append((anchor["x"], role, anchor))
    flat.sort(key=lambda item: item[0])
    bands = []
    for index, (x, role, anchor) in enumerate(flat):
        left_margin = 150 if role == "ship_to" else 65
        previous_x = flat[index - 1][0] if index else None
        next_x = flat[index + 1][0] if index + 1 < len(flat) else None
        same_column_gap = page_width * 0.12
        low = max(0, x - left_margin)
        if (
            previous_x is not None
            and x - previous_x > same_column_gap
            and abs(anchor["y"] - flat[index - 1][2]["y"]) < 150
        ):
            low = (previous_x + x) / 2
        high = min(page_width, x + 320)
        if (
            next_x is not None
            and next_x - x > same_column_gap
            and abs(anchor["y"] - flat[index + 1][2]["y"]) < 150
        ):
            high = (x + next_x) / 2
        # A lone delivery heading in the left column is frequently paired
        # with an unlabelled supplier block on the right.  Do not let the
        # delivery band swallow that second column merely because it has no
        # explicit role heading of its own.
        has_right_column_anchor = any(item_x > page_width * 0.45 for item_x, _, _ in flat)
        if x < page_width * 0.35 and not has_right_column_anchor:
            high = min(high, page_width * 0.42)
        bands.append((low, high, role, anchor))
    return bands


def _anchor_party_v3(rows, anchor, low, high):
    def clean_party(value):
        # A supplier CEDEX token immediately left of a delivery column can be
        # grouped on the recipient baseline by sparse OCR.  CEDEX is routing
        # metadata, never a recipient prefix.
        return re.sub(r"^(?:FRANCE\s+)?CEDEX\s+", "", value, flags=re.I).strip()

    for row in rows:
        if abs(row["cy"] - anchor["y"]) > 4:
            continue
        trailing = [
            token for token in row["tokens"]
            if token["bbox"][0] > anchor["bbox"][2]
        ]
        street_index = next(
            (index for index, token in enumerate(trailing) if _norm(token["text"]) in STREET_TYPES),
            None,
        )
        if street_index is not None:
            party_tokens = trailing[: max(0, street_index - 1)]
            party_tokens = [
                token for token in party_tokens
                if _norm(token["text"]) not in {"A", "LA", "LE", "SOCIETE"}
            ]
            text = " ".join(token["text"].strip(" ,:") for token in party_tokens).strip()
            if text:
                return clean_party(text)
    for row in rows:
        if 0 < row["cy"] - anchor["y"] < 45:
            column = [token for token in row["tokens"] if low <= token["bbox"][0] <= high]
            if not column:
                continue
            if any(_postal_value_v3(token) for token in column):
                continue
            if any(_norm(token["text"]) in STREET_TYPES for token in column):
                continue
            text = " ".join(token["text"] for token in column).strip()
            if text:
                return clean_party(text)
    return None


def _anchor_department_v3(rows, anchor, low, high, street_y, party_name, street_x=None):
    """Return a second clean recipient/service line before the street.

    Delivery blocks often print a site name followed by a short department
    code (for example ``CPS``).  Preserve that line while excluding contact,
    routing and structured address rows which are handled separately.
    """
    party_norm = _norm(party_name)
    if street_x is not None:
        for row in rows:
            if abs(row["cy"] - street_y) > 4:
                continue
            column = [
                token for token in row["tokens"]
                if low <= token["bbox"][0] <= high
                and token["bbox"][2] < street_x - 1
            ]
            text = " ".join(token["text"] for token in column).strip(" ,;:-")
            normalized = _norm(text)
            if (
                normalized
                and normalized != party_norm
                and len(text) <= 100
                and any(char.isalpha() for char in text)
                and not any(
                    _norm(token["text"])
                    in {
                        "BP", "CS", "TSA", "CEDEX", "FRANCE", "TEL", "TELEPHONE",
                        "FAX", "EMAIL", "MAIL", "CONTACT",
                    }
                    for token in column
                )
            ):
                return text
    for row in rows:
        if not (anchor["y"] < row["cy"] < street_y):
            continue
        column = [
            token for token in row["tokens"]
            if low <= token["bbox"][0] <= high
        ]
        if not column:
            continue
        norms = [_norm(token["text"]) for token in column]
        text = " ".join(token["text"] for token in column).strip()
        normalized = _norm(text)
        if not normalized or normalized == party_norm:
            continue
        if any(norm in STREET_TYPES or norm in ZONE_PREFIXES for norm in norms):
            continue
        if any(norm in {
            "BP", "CS", "TSA", "CEDEX", "FRANCE", "TEL", "TELEPHONE",
            "FAX", "EMAIL", "MAIL", "CONTACT",
        }
               for norm in norms):
            continue
        if any(_postal_value_v3(token) for token in column):
            continue
        if len(text) <= 100 and any(char.isalpha() for char in text):
            return text
    return None


def _site_tail_v3(tokens):
    """Extract a zone/site phrase even when it follows a routing code."""
    for index, token in enumerate(tokens):
        if _norm(token["text"]) not in ZONE_PREFIXES | BUSINESS_PARK_PREFIXES:
            continue
        selected = [token]
        previous = token
        for follower in tokens[index + 1:]:
            if follower["bbox"][0] - previous["bbox"][2] > 35:
                break
            if (
                _norm(follower["text"]) in {"BP", "CS", "TSA", "TEL", "FAX", "VIREMENT"}
                or _postal_value_v3(follower)
            ):
                break
            selected.append(follower)
            previous = follower
        return " ".join(item["text"] for item in selected)
    return None


def _clean_city_v3(value):
    return re.sub(r"^[^\w]+", "", str(value or ""), flags=re.UNICODE).strip()


def _locality_v3(row, anchor_x, neighbors):
    def is_routing_number(token):
        left = [
            candidate for candidate in row["tokens"]
            if candidate["bbox"][2] <= token["bbox"][0] + 1
        ]
        if not left:
            return False
        previous = max(left, key=lambda candidate: candidate["bbox"][2])
        gap = token["bbox"][0] - previous["bbox"][2]
        return gap <= 20 and _norm(previous["text"]) in {"BP", "CS", "TSA"}

    postals = [
        token
        for token in _postal_tokens_v3(row)
        if abs(token["bbox"][0] - anchor_x) <= 190 and not is_routing_number(token)
    ]
    if not postals:
        return None
    postal = min(postals, key=lambda token: abs(token["bbox"][0] - anchor_x))
    column_right = postal["bbox"][0] + 190
    search = [
        token for token in row["tokens"]
        if postal["bbox"][2] - 1 < token["bbox"][0] <= column_right
    ]
    for neighbor in neighbors:
        if abs(neighbor["cy"] - row["cy"]) <= 10:
            search += [
                token for token in neighbor["tokens"]
                if postal["bbox"][2] - 1 < token["bbox"][0] <= column_right
            ]
    search = sorted(
        search,
        key=lambda token: (
            abs(((token["bbox"][1] + token["bbox"][3]) / 2) - row["cy"]),
            token["bbox"][0],
        ),
    )
    city = []
    if postal.get("attached_city"):
        attached = dict(postal)
        attached["text"] = postal["attached_city"]
        city.append(attached)
    cedex = None
    previous = postal
    for token in search:
        gap = token["bbox"][0] - previous["bbox"][2]
        norm = _norm(token["text"])
        if norm == "CEDEX":
            cedex = token
            break
        if gap > 100 and not city:
            continue
        if city and gap > 35:
            break
        if norm in {"FRANCE", "EUR", "TEL", "FAX"}:
            break
        if re.fullmatch(r"\d+", token["text"].strip()):
            continue
        if any(char.isalpha() for char in token["text"]):
            city.append(token)
            previous = token
    return (
        {
            "postal": postal,
            "postal_value": _postal_value_v3(postal),
            "city": city,
            "cedex": cedex,
            "row": row,
        }
        if city
        else None
    )


def _addresses_v3(rows):
    if not rows:
        return []
    anchors = _role_anchors_v3(rows)
    page_width = rows[0]["tokens"][0]["page_width"]
    bands = _anchor_bands_v3(anchors, page_width)
    out = []
    street_rows = [segment for row in rows for segment in _row_segments(row)]

    for row in street_rows:
        street = _street_v3(row)
        if not street:
            continue
        x = street["bbox"][0]
        y = row["cy"]
        role = "unknown"
        best = None
        for low, high, candidate_role, anchor in bands:
            delta_y = y - anchor["y"]
            if low - 20 <= x <= high + 20 and -8 <= delta_y <= 125:
                score = max(delta_y, 0) + 0.15 * abs(x - anchor["x"])
                if best is None or score < best[0]:
                    best = (score, candidate_role, anchor, low, high)
        if best:
            role = best[1]
        elif x > page_width * 0.48 and anchors:
            role = "supplier"

        # On an inline address the postal code follows the complete street and
        # can be far from its starting x coordinate (long street names).  The
        # street's right edge is the reliable same-row locality anchor.
        locality = _locality_v3(row, street["bbox"][2], [])
        locality_index = row["index"] if locality else None
        for candidate_row in rows[row["index"] + 1 : row["index"] + 7]:
            if locality:
                break
            candidate = _locality_v3(
                candidate_row,
                x,
                rows[candidate_row["index"] + 1 : candidate_row["index"] + 3],
            )
            if candidate:
                locality = candidate
                locality_index = candidate_row["index"]
                break

        inline_site_tail = _site_tail_v3(row["tokens"])
        inline_site_norm = _norm(inline_site_tail.split()[0]) if inline_site_tail else ""
        zone = inline_site_tail if inline_site_norm in ZONE_PREFIXES else None
        business_park = (
            inline_site_tail if inline_site_norm in BUSINESS_PARK_PREFIXES else None
        )
        address_complement = None
        building = None
        extras = []
        for index, norm in enumerate(row["norms"][:-1]):
            if (
                norm in {"BP", "CS", "TSA"}
                and re.fullmatch(r"\d{1,6}", row["tokens"][index + 1]["text"].strip())
            ):
                extras.append((
                    norm.lower(),
                    f"{row['tokens'][index]['text']} {row['tokens'][index + 1]['text']}",
                ))
        upper = max(0, row["index"] - 2)
        lower = locality_index + 1 if locality_index is not None else row["index"] + 5
        band_low, band_high = (best[3], best[4]) if best else (max(0, x - 90), min(page_width, x + 240))
        for candidate_row in rows[upper:lower]:
            if candidate_row["index"] == row["index"]:
                continue
            column = [
                token
                for token in candidate_row["tokens"]
                if band_low <= token["bbox"][0] <= band_high
            ]
            if not column:
                continue
            norms = [_norm(token["text"]) for token in column]
            text = " ".join(token["text"] for token in column)
            site_tail = _site_tail_v3(column)
            if site_tail and any(norm in ZONE_PREFIXES for norm in norms):
                zone = site_tail
            elif site_tail and any(norm in BUSINESS_PARK_PREFIXES for norm in norms):
                business_park = site_tail
            elif text.strip().startswith("(") and text.strip().endswith(")"):
                address_complement = text
            if any(norm in BUILDING_WORDS for norm in norms):
                building = text
            for index, norm in enumerate(norms[:-1]):
                if norm in {"BP", "CS", "TSA"} and re.fullmatch(r"\d{1,6}", column[index + 1]["text"].strip()):
                    extras.append((norm.lower(), f"{column[index]['text']} {column[index + 1]['text']}"))

        components = {
            "house_number": street["house_value"],
            "house_number_suffix": street["suffix_value"],
            "street_type": street["street_type"]["text"],
            "street_name": " ".join(token["text"] for token in street["name"]),
            "industrial_zone": zone,
            "business_park": business_park,
            "address_complement": address_complement,
            "building": building,
            "postal_code": locality["postal_value"] if locality else None,
            "city": _clean_city_v3(" ".join(token["text"] for token in locality["city"])) if locality else None,
            "cedex": bool(locality and locality["cedex"]),
        }
        for key, value in extras:
            components[key] = value
        components = {key: value for key, value in components.items() if value not in (None, False, "")}

        street_text = " ".join(
            str(value)
            for value in (
                components.get("house_number"),
                components.get("house_number_suffix"),
                components.get("street_type"),
                components.get("street_name"),
            )
            if value
        )
        party_name = _anchor_party_v3(rows, best[2], best[3], best[4]) if best else None
        department = (
            _anchor_department_v3(
                rows, best[2], best[3], best[4], y, party_name, street["bbox"][0]
            )
            if best else None
        )
        parts = []
        if department:
            parts.append(department)
        if components.get("building"):
            parts.append(components["building"])
        if street_text:
            parts.append(street_text)
        if components.get("industrial_zone"):
            parts.append(components["industrial_zone"])
        if components.get("business_park"):
            parts.append(components["business_park"])
        if components.get("address_complement"):
            parts.append(components["address_complement"])
        for key in ("bp", "cs", "tsa"):
            if components.get(key):
                parts.append(components[key])
        locality_text = " ".join(
            str(value)
            for value in (
                components.get("postal_code"),
                components.get("city"),
                "CEDEX" if components.get("cedex") else None,
            )
            if value
        )
        if locality_text:
            parts.append(locality_text)

        out.append(
            {
                "role": role,
                "party_name": party_name,
                "department": department,
                "components": components,
                "formatted_address_suggestion": ", ".join(parts),
                "confidence": 0.995 if locality else 0.97,
                "source": "geometry_address_v3",
                "requires_review": True,
                "source_line": row["index"],
            }
        )

    # Anchored address sections can be valid without a street, e.g. a CS/postal billing block.
    for role, anchor_list in anchors.items():
        for anchor in anchor_list:
            band = next(
                (
                    (low, high)
                    for low, high, candidate_role, candidate_anchor in bands
                    if candidate_role == role and candidate_anchor is anchor
                ),
                (max(0, anchor["x"] - 180), min(page_width, anchor["x"] + 220)),
            )
            low, high = band
            if any(
                item["role"] == role and item["components"].get("postal_code")
                for item in out
            ):
                continue
            candidates = []
            for row in rows:
                if 0 < row["cy"] - anchor["y"] < 120:
                    column = [
                        token
                        for token in row["tokens"]
                        if low <= token["bbox"][0] <= high + 50
                    ]
                    if column:
                        candidates.append((row, column))
            postal = None
            city = None
            cedex = None
            extras = []
            party_name = None
            industrial_zone = None
            business_park = None
            address_complement = None
            building = None
            for _, column in candidates:
                candidate_row = {"tokens": column}
                routing_value_x = {
                    round(column[index + 1]["bbox"][0], 2)
                    for index, token in enumerate(column[:-1])
                    if _norm(token["text"]) in {"BP", "CS", "TSA"}
                }
                reconstructed_postals = [
                    token for token in _postal_tokens_v3(candidate_row)
                    if round(token["bbox"][0], 2) not in routing_value_x
                ]
                postal_by_start = {
                    round(token["bbox"][0], 2): token
                    for token in reconstructed_postals
                }
                for index, token in enumerate(column):
                    postal_token = postal_by_start.get(round(token["bbox"][0], 2))
                    if postal_token and postal is None:
                        consumed_right = set(postal_token.get("reconstructed_from") or [])
                        postal = token
                        if postal_token.get("reconstructed_from"):
                            postal = postal_token
                        city_tokens = []
                        if postal_token.get("attached_city"):
                            attached = dict(postal_token)
                            attached["text"] = postal_token["attached_city"]
                            city_tokens.append(attached)
                        for follower in column[index + 1 :]:
                            if (
                                consumed_right
                                and follower["text"].strip() in consumed_right
                                and follower["bbox"][2] <= postal_token["bbox"][2] + 1
                            ):
                                continue
                            norm = _norm(follower["text"])
                            if norm == "CEDEX":
                                cedex = follower
                                break
                            if norm in {"FR", "FRANCE"}:
                                break
                            if any(char.isalpha() for char in follower["text"]):
                                city_tokens.append(follower)
                        if city_tokens:
                            city = city_tokens
                    norm = _norm(token["text"])
                    if (
                        norm in {"BP", "CS", "TSA"}
                        and index + 1 < len(column)
                        and re.fullmatch(r"\d{1,6}", column[index + 1]["text"].strip())
                    ):
                        extras.append((norm.lower(), f"{token['text']} {column[index + 1]['text']}"))
                column_norms = [_norm(token["text"]) for token in column]
                column_text = " ".join(token["text"] for token in column).strip()
                site_tail = _site_tail_v3(column)
                if site_tail and any(norm in ZONE_PREFIXES for norm in column_norms):
                    industrial_zone = site_tail
                elif site_tail and any(norm in BUSINESS_PARK_PREFIXES for norm in column_norms):
                    business_park = site_tail
                elif any(norm in BUILDING_WORDS for norm in column_norms):
                    building = column_text
                elif (
                    party_name is not None
                    and column_text.startswith("(")
                    and column_text.endswith(")")
                ):
                    address_complement = column_text
                if (
                    party_name is None
                    and column
                    and not any(_postal_value_v3(token) for token in column)
                    and not any(_norm(token["text"]) in STREET_TYPES for token in column)
                ):
                    party_text = " ".join(token["text"] for token in column)
                    if site_tail:
                        party_text = re.sub(
                            re.escape(site_tail), "", party_text, count=1, flags=re.I
                        ).strip(" ,;|-/")
                    if party_text and _norm(party_text) not in {"FR", "FRANCE"}:
                        party_name = party_text
            if postal and city:
                components = {
                    "postal_code": _postal_value_v3(postal),
                    "city": _clean_city_v3(" ".join(token["text"] for token in city)),
                    "cedex": bool(cedex),
                    "industrial_zone": industrial_zone,
                    "business_park": business_park,
                    "address_complement": address_complement,
                    "building": building,
                }
                components = {
                    key: value for key, value in components.items()
                    if value not in (None, False, "")
                }
                for key, value in extras:
                    components[key] = value
                parts = []
                if party_name:
                    parts.append(party_name)
                for key in ("building", "industrial_zone", "business_park", "address_complement"):
                    if components.get(key):
                        parts.append(components[key])
                for key in ("bp", "cs", "tsa"):
                    if components.get(key):
                        parts.append(components[key])
                parts.append(
                    " ".join(
                        value
                        for value in (
                            components.get("postal_code"),
                            components.get("city"),
                            "CEDEX" if components.get("cedex") else None,
                        )
                        if value
                    )
                )
                out.append(
                    {
                        "role": role,
                        "party_name": party_name,
                        "components": components,
                        "formatted_address_suggestion": ", ".join(parts),
                        "confidence": 0.985,
                        "source": "geometry_address_section_v3",
                        "requires_review": True,
                    }
                )

    # Remove weak footer/legal noise and deduplicate identical addresses across roles.
    filtered = []
    for item in out:
        components = item.get("components", {})
        if (
            item.get("role") in {"supplier", "unknown"}
            and not components.get("postal_code")
            and not components.get("house_number")
        ):
            continue
        if item.get("role") == "unknown" and item.get("source_line", 0) > int(len(rows) * 0.60):
            continue
        filtered.append(item)

    rank = {"ship_to": 4, "bill_to": 3, "supplier": 2, "unknown": 1}
    best_by_address = {}
    for item in filtered:
        key = _norm(item["formatted_address_suggestion"])
        if key not in best_by_address or rank.get(item["role"], 0) > rank.get(best_by_address[key]["role"], 0):
            best_by_address[key] = item
    return list(best_by_address.values())


def extract_geometry_suggestions(block_lines):
    rows = visual_lines(block_lines)
    return {
        "anchored_fields": _anchored_fields_v3(rows),
        "address_candidates": _addresses_v3(rows),
        "geometry_version": "visual-lines-v3",
    }
