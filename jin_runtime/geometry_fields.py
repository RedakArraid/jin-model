from __future__ import annotations
import re, unicodedata
from typing import Any

STREET_TYPES={'RUE','AVENUE','AV','BOULEVARD','BD','ROUTE','RTE','CHEMIN','CHE','ALLEE','IMPASSE','PLACE','QUAI','COURS','PASSAGE','SQUARE','VOIE','RONDPOINT','MONTEE','TRAVERSE'}
ZONE_PREFIXES={'ZI','ZA','ZAC','ZAE','PARC','ZONE'}
BUILDING_WORDS={'BATIMENT','BAT','IMMEUBLE','RESIDENCE','ENTREE','ESCALIER'}
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
    "ship_to": ["ADRESSE DE LIVRAISON", "A LIVRER A"],
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
        for index, norm in enumerate(norms):
            if norm in {"DOCUMENT", "PIECE"}:
                headers.append(("order_number", tokens[index]))
            elif norm == "COMMANDE" and "DATE" in norms and "CLIENT" in norms:
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
                        valid = (
                            bool(DATE_RE.fullmatch(raw))
                            if key == "order_date"
                            else len(_norm(raw)) >= 5
                            and any(char.isdigit() for char in _norm(raw))
                        )
                        if valid and distance < 70 and (best is None or distance < best[0]):
                            best = (distance, token, raw)
                if best:
                    out.setdefault(key, _field(best[2], best[1]["bbox"], best[1], 0.995))

    # Summary tables: NET H.T., TOTAL H.T., VALEUR/MONTANT TVA, TOTAL/MONTANT TTC, NET A PAYER.
    total_candidates = {}
    for row in rows:
        norms = row["norms"]
        tokens = row["tokens"]
        mappings = []
        for index, norm in enumerate(norms):
            if norm == "NET" and index + 1 < len(norms) and norms[index + 1] == "HT":
                mappings.append(("total_net", (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2, 3))
            if norm == "TOTAL" and index + 1 < len(norms) and norms[index + 1] in {"HT", "TTC"}:
                mappings.append((
                    "total_net" if norms[index + 1] == "HT" else "total_gross",
                    (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2,
                    3,
                ))
            if norm in {"MONTANT", "VALEUR"} and index + 1 < len(norms) and norms[index + 1] == "TVA":
                mappings.append(("total_vat", (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2, 3))
            if norm == "MONTANT" and index + 1 < len(norms) and norms[index + 1] == "TTC":
                mappings.append(("total_gross", (tokens[index]["bbox"][0] + tokens[index + 1]["bbox"][2]) / 2, 3))
            if norm == "NET" and index + 2 < len(norms) and norms[index + 1] == "A" and norms[index + 2] == "PAYER":
                mappings.append(("amount_due", (tokens[index]["bbox"][0] + tokens[index + 2]["bbox"][2]) / 2, 3))
        for key, x, depth in mappings:
            candidate = _nearest_numeric_below_v3(rows, row, x, depth)
            if candidate:
                score = (row["cy"], -candidate[0])
                if key not in total_candidates or score > total_candidates[key][0]:
                    total_candidates[key] = (score, candidate)
    for key, (_, candidate) in total_candidates.items():
        _, value, box, token = candidate
        out.setdefault(key, _field(value, box, token, 0.995))

    if "total_gross" not in out:
        summary_rows = []
        for row in rows:
            groups = []
            current = []
            last_x = None
            for token in row["tokens"]:
                raw = token["text"].strip()
                if re.fullmatch(r"[0-9][0-9\s\u00a0\u202f.,]*", raw):
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

            amounts = []
            for group in groups:
                value = _numeric_value_v3(group)
                if value and ("," in value or "." in value):
                    amounts.append((group, value))
            if len(amounts) >= 2:
                summary_rows.append((row["cy"], amounts))

        if summary_rows:
            _, amounts = max(summary_rows, key=lambda item: item[0])
            group, value = max(
                amounts,
                key=lambda item: item[0][-1]["bbox"][2],
            )
            box = _union_bbox_v3(group)
            out["total_gross"] = _field(value, box, group[0], 0.97)

    return out


def _house_before_v3(tokens, index):
    # Prefer explicit ranges before a single previous number.
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
        if re.fullmatch(r"\d{1,4}(?:\s*[,\-/]\s*\d{1,4})?", raw):
            normalized = raw.replace(",", "-").replace("/", "-").replace(" ", "")
            return normalized, None, [tokens[index - 1]]
    if (
        index > 1
        and _norm(tokens[index - 1]["text"]) in {"BIS", "TER", "QUATER"}
        and re.fullmatch(r"\d{1,4}", tokens[index - 2]["text"].strip())
    ):
        return (
            tokens[index - 2]["text"].strip(),
            tokens[index - 1]["text"].strip(),
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
    return out


def _anchor_bands_v3(anchors, page_width):
    flat = []
    for role, items in anchors.items():
        for anchor in items:
            flat.append((anchor["x"], role, anchor))
    flat.sort(key=lambda item: item[0])
    bands = []
    for index, (x, role, anchor) in enumerate(flat):
        low = max(0, x - 65) if index == 0 else (flat[index - 1][0] + x) / 2
        high = min(page_width, x + 320) if index == len(flat) - 1 else (x + flat[index + 1][0]) / 2
        bands.append((low, high, role, anchor))
    return bands


def _anchor_party_v3(rows, anchor, low, high):
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
                return text
    return None


def _locality_v3(row, anchor_x, neighbors):
    postals = [
        token
        for token in row["tokens"]
        if _postal_value_v3(token) and abs(token["bbox"][0] - anchor_x) <= 190
    ]
    if not postals:
        return None
    postal = min(postals, key=lambda token: abs(token["bbox"][0] - anchor_x))
    search = [token for token in row["tokens"] if token["bbox"][0] > postal["bbox"][2] - 1]
    for neighbor in neighbors:
        if abs(neighbor["cy"] - row["cy"]) <= 10:
            search += [token for token in neighbor["tokens"] if token["bbox"][0] > postal["bbox"][2] - 1]
    search = sorted(
        search,
        key=lambda token: (
            abs(((token["bbox"][1] + token["bbox"][3]) / 2) - row["cy"]),
            token["bbox"][0],
        ),
    )
    city = []
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

        locality = None
        locality_index = None
        for candidate_row in rows[row["index"] + 1 : row["index"] + 7]:
            candidate = _locality_v3(
                candidate_row,
                x,
                rows[candidate_row["index"] + 1 : candidate_row["index"] + 3],
            )
            if candidate:
                locality = candidate
                locality_index = candidate_row["index"]
                break

        zone = None
        building = None
        extras = []
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
            if norms and norms[0] in ZONE_PREFIXES:
                zone = text
            if any(norm in BUILDING_WORDS for norm in norms):
                building = text
            for index, norm in enumerate(norms[:-1]):
                if norm in {"BP", "CS", "TSA"} and re.fullmatch(r"\d{3,6}", column[index + 1]["text"].strip()):
                    extras.append((norm.lower(), f"{column[index]['text']} {column[index + 1]['text']}"))

        components = {
            "house_number": street["house_value"],
            "house_number_suffix": street["suffix_value"],
            "street_type": street["street_type"]["text"],
            "street_name": " ".join(token["text"] for token in street["name"]),
            "industrial_zone": zone,
            "building": building,
            "postal_code": locality["postal_value"] if locality else None,
            "city": " ".join(token["text"] for token in locality["city"]) if locality else None,
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
        parts = []
        if components.get("building"):
            parts.append(components["building"])
        if street_text:
            parts.append(street_text)
        if components.get("industrial_zone"):
            parts.append(components["industrial_zone"])
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

        party_name = _anchor_party_v3(rows, best[2], best[3], best[4]) if best else None
        out.append(
            {
                "role": role,
                "party_name": party_name,
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
            for _, column in candidates:
                for index, token in enumerate(column):
                    if _postal_value_v3(token):
                        postal = token
                        city_tokens = []
                        for follower in column[index + 1 :]:
                            norm = _norm(follower["text"])
                            if norm == "CEDEX":
                                cedex = follower
                                break
                            if any(char.isalpha() for char in follower["text"]):
                                city_tokens.append(follower)
                        if city_tokens:
                            city = city_tokens
                    norm = _norm(token["text"])
                    if (
                        norm in {"BP", "CS", "TSA"}
                        and index + 1 < len(column)
                        and re.fullmatch(r"\d{3,6}", column[index + 1]["text"].strip())
                    ):
                        extras.append((norm.lower(), f"{token['text']} {column[index + 1]['text']}"))
                if (
                    party_name is None
                    and column
                    and not any(_postal_value_v3(token) for token in column)
                    and not any(_norm(token["text"]) in STREET_TYPES for token in column)
                ):
                    party_name = " ".join(token["text"] for token in column)
            if postal and city:
                components = {
                    "postal_code": _postal_value_v3(postal),
                    "city": " ".join(token["text"] for token in city),
                    "cedex": bool(cedex),
                }
                for key, value in extras:
                    components[key] = value
                parts = []
                if party_name:
                    parts.append(party_name)
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
