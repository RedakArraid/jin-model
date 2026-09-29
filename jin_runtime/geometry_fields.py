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
                    n=_norm(t['text'])
                    if t['bbox'][0]>ax and len(n)>=7 and ORDER_CODE_RE.fullmatch(t['text'].strip()) and any(c.isdigit() for c in n) and any(c.isalpha() for c in n):
                        out.setdefault('order_number',_field(t['text'].strip(),t['bbox'],t,.995));break
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

def addresses(rows):
    anchors={}
    for r in rows:
        txt=_deaccent(r['text']).upper()
        if 'ADRESSE DE LIVRAISON' in txt:anchors['ship_to']={'y':r['cy']}
        if 'FACTURE A' in txt or 'FACTUREE A' in txt:anchors['bill_to']={'y':r['cy']}
    if not rows:return []
    pw=rows[0]['tokens'][0]['page_width'];out=[]
    for row in rows:
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
