"""Source-backed draft review, with a per-field audit trail and residual questions.

Plan from an immutable snapshot first. Apply inside Linux only, checking the
annotation digest again. No training validation, no model confidence promotion,
and no absence inferred merely from an unsuccessful text search.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
from hashlib import sha256

import fitz

from jin_runtime.studio_contract import training_annotation_issues, normalize_studio_annotation
from scripts.studio_annotation_pdf_audit import folded, normalized, value_matches, nearby_text
from scripts.studio_annotation_locator import lookup_keys, indexed_occurrences, page_context, block_role, column_role, context_accepts
from scripts.studio_table_ambiguity_review import decimal_value

ROOT = Path('data/evaluation/annotation-complete-review-20261116')
PENDING = {'AMBIGUOUS', 'UNREVIEWED'}
ACTIVE = {'CONFIRMED', 'CORRECTED'}
ATOMIC = {'HOUSE_NUMBER','HOUSE_NUMBER_SUFFIX','STREET_TYPE','STREET_NAME'}
LINE = re.compile(r'order\.line_items\[(\d+)\]')


def plan_digest(plan):
    return sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()


def assert_audited(plan,audit):
    if audit.get('plan_digest')!=plan_digest(plan):
        raise ValueError('Plan changed since independent evidence audit')
    if any(audit.get(k) for k in ('increased_training_issues','invalid_geometry','native_evidence_disagreements')):
        raise ValueError('Evidence/geometry audit failed; do not save this plan')


def value(field):
    current = field.get('corrected_value')
    if current is None:
        current = field.get('extracted_value')
    return '' if current is None else str(current)


def box(words):
    assert words and len({w['page'] for w in words}) == 1
    return {'page':words[0]['page'], 'bbox':[
        math.floor(min(w['bbox'][0] for w in words)), math.floor(min(w['bbox'][1] for w in words)),
        math.ceil(max(w['bbox'][2] for w in words)), math.ceil(max(w['bbox'][3] for w in words))]}


def text(words):
    return ' '.join(w['text'] for w in words)


def native_words(pdf):
    words = []
    for p in pdf:
        for w in p.get_text('words'):
            if not (0 <= w[0] < w[2] <= p.rect.width and 0 <= w[1] < w[3] <= p.rect.height):
                continue
            words.append({'text':w[4], 'page':p.number+1, 'source':'native', 'confidence':100,
                'group':(w[5],w[6]), 'bbox':[w[0]/p.rect.width*1000,w[1]/p.rect.height*1000,
                                           w[2]/p.rect.width*1000,w[3]/p.rect.height*1000]})
    return words


def rows(words):
    groups = defaultdict(list)
    for w in words:
        groups[(w['page'],w.get('source'),w.get('crop'),tuple(w.get('group',[])))].append(w)
    return [sorted(group,key=lambda w:w['bbox'][0]) for group in groups.values()]


def matches(words, wanted, *, minimum_confidence=85):
    """Exact contiguous words, never a fuzzy edit-distance match."""
    key = normalized(wanted)
    found = []
    if not key:
        return found
    for line in rows(words):
        for start in range(len(line)):
            for stop in range(start+1,min(start+20,len(line))+1):
                phrase = line[start:stop]
                if any(w.get('confidence',100)<minimum_confidence for w in phrase):
                    break
                if len(phrase)>1 and phrase[-1]['bbox'][0]-phrase[-2]['bbox'][2]>45:
                    break
                actual = normalized(text(phrase))
                if actual == key:
                    found.append(phrase)
                    break
                if len(actual)>len(key):
                    break
    return found


def set_source(field, source, words_or_evidence, reason, *, force=False):
    if not force and field.get('status') not in PENDING:
        return False
    evidence = box(words_or_evidence) if isinstance(words_or_evidence,list) else words_or_evidence
    if not evidence or not evidence.get('bbox'):
        return False
    equal = normalized(value(field)) == normalized(source)
    numeric = field['label'].startswith('TOTAL_') or field['label'] in {
        'LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE','LINE_ITEM_TOTAL','ADDITIONAL_CHARGE_AMOUNT'}
    if numeric:
        if decimal_value(source) is None:
            return False
        equal = decimal_value(value(field)) == decimal_value(source)
    if not equal:
        field['corrected_value'] = str(decimal_value(source)) if numeric else str(source)
    field['status'] = 'CORRECTED' if field.get('corrected_value') is not None else 'CONFIRMED'
    field['evidence'] = evidence
    field['comment'] = reason + ' Préannotation assistée ; relecture humaine requise.'
    return True


def absent(field, reason):
    field['status'] = 'ABSENT'
    field['corrected_value'] = None
    field.pop('evidence',None)
    field['comment'] = reason + ' Préannotation assistée ; relecture humaine requise.'


def selected(words,page,x0,x1,y0,y1):
    return sorted([w for w in words if w['page']==page and x0 <= w['bbox'][0]<x1 and y0<=w['bbox'][1]<y1],
                  key=lambda w:(round(w['bbox'][1]/4),w['bbox'][0]))


def iserba_table(words):
    """Printed family signature plus complete rows on every page, no model values."""
    result=[]
    for page in sorted({w['page'] for w in words}):
        headers=[w for w in words if w['page']==page and folded(w['text'])=='article' and 40<w['bbox'][0]<100]
        if not headers:
            continue
        for header in headers:
            top=header['bbox'][3]
            signature=folded(text(selected(words,page,40,950,top-20,top+15)))
            if not all(term in signature for term in ('designation','prix','montant','colis')):
                continue
            totals=[w['bbox'][1] for w in words if w['page']==page and folded(w['text'])=='total' and w['bbox'][1]>top and 550<w['bbox'][0]<700]
            bottom=min(totals,default=850)
            refs=[w for w in selected(words,page,40,210,top+5,bottom) if re.fullmatch(r'(?:EL)?[0-9][A-Z0-9.\-/]{4,25}|PORT',w['text'],re.I)]
            for i,ref in enumerate(refs):
                y=ref['bbox'][1]
                end=refs[i+1]['bbox'][1]-2 if i+1<len(refs) else min(bottom-3,y+65)
                cells={'LINE_ITEM_REFERENCE':[ref], 'LINE_ITEM_DESCRIPTION':selected(words,page,215,470,y-2,end)}
                for label,a,b in [('LINE_ITEM_QUANTITY',475,550),('LINE_ITEM_UNIT_PRICE',555,630),('LINE_ITEM_TOTAL',640,730)]:
                    cells[label]=selected(words,page,a,b,y-3,y+8)
                if not all(cells.values()):
                    raise ValueError('Incomplete ISERBA row at page '+str(page))
                q,p,t=(decimal_value(text(cells[k])) for k in ('LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE','LINE_ITEM_TOTAL'))
                if any(v is None for v in (q,p,t)) or abs(q*p-t)>Decimal('.05'):
                    raise ValueError('ISERBA arithmetic/columns need visual review')
                result.append(cells)
    return result


def review_iserba(fields,words,ocr,events):
    alltext=folded(text(words))
    if not all(t in alltext for t in ('crc_appro@iserba.fr','facture','colis','qt')):
        return False
    table=iserba_table(words)
    if not table:
        return False
    products=[r for r in table if r['LINE_ITEM_REFERENCE'][0]['text'].upper()!='PORT']
    charges=[r for r in table if r['LINE_ITEM_REFERENCE'][0]['text'].upper()=='PORT']
    refs=[f for f in fields if f['label']=='LINE_ITEM_REFERENCE']
    def base(s):
        return re.sub(r'^el','',normalized(s))
    # A partial table must not be silently zipped to the wrong annotations.
    alignment={}
    cursor=0
    for f in refs:
        candidates=[i for i in range(cursor,len(products)) if base(value(f))==base(text(products[i]['LINE_ITEM_REFERENCE']))]
        if not candidates:
            events.append({'kind':'table_alignment_review','path':f['path']})
            return False
        i=candidates[0]
        alignment[int(LINE.search(f['path']).group(1))]=i
        cursor=i+1
    for f in fields:
        m=LINE.search(f['path'])
        if not m or int(m.group(1)) not in alignment:
            continue
        row=products[alignment[int(m.group(1))]]
        if f['label'] in row:
            set_source(f,text(row[f['label']]),row[f['label']], 'Cellule relue avec la référence, les colonnes et le contrôle quantité × prix.',force=True)
        elif f['label'] in {'LINE_ITEM_NUMBER','LINE_ITEM_UNIT'}:
            absent(f,'Aucune colonne de numéro ordinal/unité imprimée : « Vendu par colis de » est un conditionnement, pas cette donnée.')
    if len(products)!=len(refs):
        # Explicitly report missing products; a dedicated structural review adds them.
        events.append({'kind':'missing_product_rows','source_count':len(products),'annotation_count':len(refs)})
    for f in list(fields):
        label=f['label']
        if label in {'TOTAL_GROSS','TOTAL_AMOUNT_DUE','TOTAL_VAT'} and not re.search(r'ttc|net\s+a\s+payer|montant\s+tva',alltext):
            absent(f,'Toutes les pages portent des totaux HT uniquement ; aucun total TTC/net à payer/TVA imprimé. Ne pas recopier le HT.')
        if label in {'TOTAL_NET','TOTAL_SUBTOTAL'}:
            for page in sorted({w['page'] for w in words},reverse=True):
                for line in rows([w for w in words if w['page']==page]):
                    title=normalized(text(line))
                    wanted='totallignesht' if label=='TOTAL_SUBTOTAL' else 'totalht'
                    if title.rstrip(':')!=wanted:
                        continue
                    y=line[0]['bbox'][1]
                    amount=selected(words,page,800,904,y-3,y+5)
                    if amount and decimal_value(text(amount)) is not None:
                        set_source(f,text(amount),amount,'Montant associé à son libellé imprimé '+text(line)+'.',force=True)
        if label.startswith('ADDITIONAL_CHARGE_'):
            if not charges:
                fee_lines=[line for line in rows(words) if normalized(text(line)).startswith('fraisrem')]
                if fee_lines and all(not selected(words,line[0]['page'],800,904,line[0]['bbox'][1]-3,line[0]['bbox'][1]+5) for line in fee_lines):
                    absent(f,'Zone Frais/Rem vide sur toutes les pages ; les pièces et leur montant appartiennent aux lignes produits, pas aux frais.')
            elif len(charges)==1 and re.search(r'additional_charges\[0\]',f['path']):
                source=charges[0]['LINE_ITEM_REFERENCE' if label=='ADDITIONAL_CHARGE_LABEL' else 'LINE_ITEM_TOTAL']
                set_source(f,text(source),source,'Frais de port imprimés sur une ligne PORT distincte des produits.',force=True)
        if label == 'OFFER_NUMBER' and f['status'] in PENDING:
            candidates=matches(words,value(f))
            candidates=[c for c in candidates if c[0]['bbox'][0]>800 and c[0]['bbox'][1]>500]
            if candidates:
                set_source(f,value(f),candidates[0],"Référence imprimée dans la colonne N° d'offre / Réf devis.")
        if label.endswith('_COUNTRY_CODE') and value(f)=='FR':
            role=label[:-13]
            zones={'SUPPLIER':(460,950,270,380),'BILL_TO':(40,450,400,525),'SHIP_TO':(470,960,400,525)}
            if role in zones:
                a,b,c,d=zones[role]
                candidates=[w for w in selected(words,1,a,b,c,d) if folded(w['text'])=='france']
                if candidates:
                    country=next((g for g in fields if g['label']==role+'_COUNTRY'),None)
                    if country is None:
                        country={'label':role+'_COUNTRY','path':f'order.addresses.{role.lower()}.selected.components.country','status':'UNREVIEWED','extracted_value':None}
                        fields.append(country)
                    set_source(country,'France',[candidates[0]],'Pays explicitement imprimé dans le bloc adresse.',force=True)
                    absent(f,'Le document imprime « France », pas le code « FR ». Le nom du pays est annoté séparément ; le code serait dérivé.')
        # Raster letterhead/footer provides an independent buyer occurrence.
        if label.startswith('BUYER_') and f['status'] in PENDING:
            candidates=matches(ocr,value(f),minimum_confidence=85)
            candidates=[c for c in candidates if c[0]['bbox'][1]>880 or c[0]['bbox'][1]<150]
            if candidates:
                candidates.sort(key=lambda c:(c[0]['bbox'][1]<880,c[0]['page'],len(c)))
                set_source(f,value(f),candidates[0],"Occurrence indépendante retrouvée par OCR local dans le logo/pied de page de l'émetteur.")
    # Full streets are recovered independently from their explicit blocks.
    for role,(x0,x1,y0,y1) in {'SUPPLIER':(475,950,285,330),'SHIP_TO':(475,900,410,460)}.items():
        line_options=[line for line in rows(selected(words,1,x0,x1,y0,y1)) if re.search(
            r'^(?:\d+[a-z]*\s+)?(?:rue|avenue|av\.?|boulevard|bld|bd|route|chemin|impasse|allee|place|quai)\b',folded(text(line)))]
        if len(line_options)!=1:
            continue
        street=next((f for f in fields if f['label']==role+'_STREET'),None)
        parts=[f for f in fields if f['label'].startswith(role+'_') and f['label'][len(role)+1:] in ATOMIC]
        if not parts:
            continue
        if street is None:
            street={'label':role+'_STREET','path':f'order.addresses.{role.lower()}.selected.components.street','status':'UNREVIEWED','extracted_value':None}
            fields.append(street)
        set_source(street,text(line_options[0]),line_options[0],'Voie complète relue dans le bloc explicite de cette adresse.',force=True)
        for part in parts:
            fields.remove(part)
            events.append({'kind':'removed_street_component','field':part,'replacement_path':street['path']})
    for f in list(fields):
        if f['label'] in {'BILL_TO_VAT_NUMBER','SHIP_TO_VAT_NUMBER'} and f['status'] in PENDING:
            buyer=next((g for g in fields if g['label']=='BUYER_VAT_NUMBER' and g['status'] in ACTIVE and normalized(value(g))==normalized(value(f))),None)
            if buyer:
                fields.remove(f)
                events.append({'kind':'removed_role_duplicate','field':f,'replacement_path':buyer['path'],
                    'reason':"La TVA imprimée dans l'en-tête est celle de l'émetteur ; pas de preuve indépendante pour ce second rôle."})
    return True


def street_merges(fields,words,events):
    for role in ('BUYER','SUPPLIER','SHIP_TO','BILL_TO'):
        parts=[f for f in fields if f['label'].startswith(role+'_') and f['label'][len(role)+1:] in ATOMIC]
        street=next((f for f in fields if f['label']==role+'_STREET'),None)
        if not parts:
            continue
        lines=[]
        for original in rows(words):
            segments=[[]]
            for w in original:
                if w['text'] in {'-','|',';'}:
                    segments.append([])
                else:
                    segments[-1].append(w)
            lines.extend(s for s in segments if s)
        for line in lines:
            raw=text(line)
            if not re.search(r'\b(?:rue|avenue|av\.?|boulevard|bld|bd|route|chemin|impasse|allee|place|quai|lotissement)\b',folded(raw)) or len(raw)>120:
                continue
            # Require every non-absent component, and its own source location.
            active=[f for f in parts if f['status']!='ABSENT' and value(f)]
            if not active or not all(normalized(value(f)) in normalized(raw) for f in active):
                continue
            prior=(street or active[0]).get('evidence') or {}
            linebox=box(line)
            if prior.get('page')!=linebox['page'] or not prior.get('bbox'):
                continue
            if abs(prior['bbox'][1]-linebox['bbox'][1])>6 or abs(prior['bbox'][0]-linebox['bbox'][0])>130:
                continue
            # Avoid merging billing evidence into the buyer address.
            if role=='BUYER' and any('Facturation' in f.get('comment','') for f in active):
                continue
            if min(w.get('confidence',100) for w in line)<85:
                continue
            if street is None:
                street={'label':role+'_STREET','path':f'order.addresses.{role.lower()}.selected.components.street','status':'UNREVIEWED','extracted_value':None}
                fields.append(street)
            set_source(street,raw,line,'Voie complète imprimée ; remplace les composants atomiques.',force=True)
            for part in parts:
                fields.remove(part)
                events.append({'kind':'removed_street_component','field':part,'replacement_path':street['path']})
            break


def deduplicate_explicit_roles(fields,words,context,events):
    """Do not mislabel a billing token as buyer; retain its explicit annotation."""
    for f in list(fields):
        if not f['label'].startswith('BUYER_') or f['status'] not in PENDING:
            continue
        e=f.get('evidence') or {}
        if not e.get('bbox') or block_role(context,e)!='BILL_TO':
            continue
        suffix=f['label'][6:]
        # Company name is a separate semantic question, not an address alias.
        if suffix in {'NAME','VAT_NUMBER'} or suffix.startswith('CONTACT_'):
            continue
        equivalent=[g for g in fields if g['label'].startswith('BILL_TO_') and g['status'] in ACTIVE and
                    (g['label'][8:]==suffix or {g['label'][8:],suffix}<={'INDUSTRIAL_ZONE','ACTIVITY_PARK'} or
                     suffix in ATOMIC and g['label']=='BILL_TO_STREET')]
        if not any(normalized(value(f)) and normalized(value(f)) in normalized(value(g)) for g in equivalent):
            continue
        # Check no independent occurrence exists, e.g. in a raster footer.
        independent=[c for c in matches(words,value(f)) if box(c)['bbox'][1]>880]
        if independent:
            continue
        fields.remove(f)
        events.append({'kind':'removed_role_duplicate','field':f,'replacement_path':equivalent[0]['path'],
                       'reason':'Le cadre désigne explicitement la facturation, déjà annotée sous son propre rôle.'})


HEADER_PATTERNS={
    'LINE_ITEM_REFERENCE':r'^(?:ref(?:erence)?\.?|article|code(?: article| fournisseur)?|reference fournisseur|ref\.\s*four\.?|codeart\.?)(?:\s.*)?$',
    'LINE_ITEM_DESCRIPTION':r'^(?:designation|description|libelle)(?:\s.*)?$',
    'LINE_ITEM_QUANTITY':r'^(?:qte\.?|quantite|quant\.?|qtee|qt\.)(?:\s.*)?$',
    'LINE_ITEM_UNIT_PRICE':r'^(?:p\.?\s*u\.?|prix(?:\s+unitaire)?|prix net|p\.u\.net)(?:\s.*)?$',
    'LINE_ITEM_TOTAL':r'^(?:montant|total|mt)(?:\s.*)?$',
    'LINE_ITEM_UNIT':r'^(?:unite|un|um|u\.?m\.?)$',
    'LINE_ITEM_NUMBER':r'^(?:poste|position|ligne|n[°o](?:\s*lig(?:ne)?\.?)?)$',
}


def table_headers(words):
    candidates=[]
    for line in rows(words):
        raw=folded(text(line)).strip()
        if len(raw)>60:
            continue
        for label,pattern in HEADER_PATTERNS.items():
            if re.match(pattern,raw):
                candidates.append({'label':label,'words':line,'evidence':box(line)})
    groups=[]
    for h in candidates:
        e=h['evidence']
        same=[g for g in candidates if g['evidence']['page']==e['page'] and abs(g['evidence']['bbox'][1]-e['bbox'][1])<16]
        labels={g['label'] for g in same}
        if len(labels)>=3 and 'LINE_ITEM_QUANTITY' in labels and 'LINE_ITEM_DESCRIPTION' in labels:
            signature=tuple(sorted((g['label'],tuple(g['evidence']['bbox'])) for g in same))
            if not any(x['signature']==signature for x in groups):
                groups.append({'page':e['page'],'top':max(g['evidence']['bbox'][3] for g in same),'headers':same,'signature':signature})
    return groups


def table_column(table,evidence):
    x=(evidence['bbox'][0]+evidence['bbox'][2])/2
    options=[]
    for h in table['headers']:
        a,_,b,_=h['evidence']['bbox']
        # Long designations extend far beyond their header: compare left edge.
        dist=abs(evidence['bbox'][0]-a) if h['label']=='LINE_ITEM_DESCRIPTION' else abs(x-(a+b)/2)
        options.append((dist,h['label']))
    distance,label=min(options)
    return label if distance<75 else ''


def review_native_tables(fields,pdf,words):
    tables=table_headers(words)
    if not tables:
        return
    keys=set().union(*(lookup_keys(f) for f in fields if LINE.search(f['path'])))
    index=indexed_occurrences(pdf,keys)
    anchors={}
    used=set()
    for f in fields:
        if f['label']!='LINE_ITEM_REFERENCE':
            continue
        found=[]
        for k in lookup_keys(f):
            for c in index.get(k,[]):
                e=c['evidence']
                if len(normalized(value(f)))<5 or not re.search(r'\d',c['text']):
                    continue
                for table in tables:
                    if table['page']==e['page'] and table['top']<e['bbox'][1]<850 and table_column(table,e)=='LINE_ITEM_REFERENCE':
                        found.append((e['page'],e['bbox'][1],c,table))
        found.sort(key=lambda c:c[:2])
        found=[c for c in found if (c[0],tuple(c[2]['evidence']['bbox'])) not in used]
        if not found:
            continue
        page,_,c,table=found[0]
        # Duplicate references in consecutive lines are matched in source order.
        used.add((page,tuple(c['evidence']['bbox'])))
        anchors[int(LINE.search(f['path']).group(1))]=(c,table)
        set_source(f,c['text'],c['evidence'],'Référence retrouvée dans la colonne article du tableau, dans son ordre imprimé.')
    for f in fields:
        m=LINE.search(f['path'])
        if not m or f['status'] not in PENDING or int(m.group(1)) not in anchors:
            continue
        anchor,table=anchors[int(m.group(1))]
        ae=anchor['evidence']
        if f['label']=='LINE_ITEM_NUMBER':
            # Do not infer absence from parser failure; need an explicit leftmost
            # article column and no number/position column in the printed header.
            left=min(table['headers'],key=lambda h:h['evidence']['bbox'][0])
            if left['label']=='LINE_ITEM_REFERENCE' and left['evidence']['bbox'][0]<110 and not any(h['label']=='LINE_ITEM_NUMBER' for h in table['headers']):
                absent(f,'Le tableau commence par la référence article, sans colonne de numéro ordinal. La numérotation JSON est générée, non imprimée.')
            continue
        candidates={}
        for k in lookup_keys(f):
            for c in index.get(k,[]):
                e=c['evidence']
                if e['page']!=ae['page'] or abs(e['bbox'][1]-ae['bbox'][1])>8:
                    continue
                if table_column(table,e)!=f['label'] or not value_matches(f['label'],value(f),c['text']):
                    continue
                if f['label'] in {'LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE','LINE_ITEM_TOTAL'} and decimal_value(c['text'])!=decimal_value(value(f)):
                    continue
                if f['label']=='LINE_ITEM_DESCRIPTION' and normalized(c['text'])!=normalized(value(f)):
                    continue
                candidates[(e['page'],tuple(e['bbox']))]=c
        if len(candidates)==1:
            c=next(iter(candidates.values()))
            set_source(f,value(f),c['evidence'],'Cellule retrouvée sur la même ligne que la référence et sous son en-tête de colonne.')


def review_inspected_native_layouts(document_id,fields,pdf,words,events):
    """Two rendered layouts whose headings are graphic or unusually offset."""
    if document_id=='jdoc-3665359d28b7474f92c92535e70841f7':
        anchors=[w for w in words if w['page']<=2 and 60<w['bbox'][0]<170 and 420<w['bbox'][1]<840 and re.fullmatch(r'\d[A-Z0-9]{8,11}',w['text'])]
        anchors.sort(key=lambda w:(w['page'],w['bbox'][1]))
        refs=[f for f in fields if f['label']=='LINE_ITEM_REFERENCE']
        if len(anchors)!=27 or len(refs)!=27:
            events.append({'kind':'table_alignment_review','reason':'GARANKA: expected 27 physical rows'})
            return
        table=[]
        for a in anchors:
            page,y=a['page'],a['bbox'][1]
            cells={
                'LINE_ITEM_REFERENCE':selected(words,page,45,200,y-2,y+5),
                'LINE_ITEM_DESCRIPTION':selected(words,page,205,570,y-2,y+5),
                'LINE_ITEM_QUANTITY':selected(words,page,700,745,y-2,y+5),
                'LINE_ITEM_UNIT_PRICE':selected(words,page,800,875,y-2,y+5),
            }
            if not all(cells.values()) or any(decimal_value(text(cells[k])) is None for k in ('LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE')):
                events.append({'kind':'table_alignment_review','reason':'GARANKA: incomplete cell'})
                return
            table.append(cells)
        # Independent document total reconciles all printed quantities/prices.
        calculated=sum(decimal_value(text(r['LINE_ITEM_QUANTITY']))*decimal_value(text(r['LINE_ITEM_UNIT_PRICE'])) for r in table)
        if calculated!=Decimal('2389.45'):
            events.append({'kind':'table_alignment_review','reason':'GARANKA: sum mismatch'})
            return
        for f in fields:
            m=LINE.search(f['path'])
            if m and int(m.group(1))<len(table):
                cells=table[int(m.group(1))]
                if f['label'] in cells:
                    set_source(f,text(cells[f['label']]),cells[f['label']],'Tableau GARANKA relu : colonne Commandé distincte du lot, somme des 27 lignes = 2389,45.',force=True)
                elif f['label'] in {'LINE_ITEM_NUMBER','LINE_ITEM_TOTAL','LINE_ITEM_UNIT'}:
                    absent(f,'Pas de numéro ordinal, de montant par ligne ou d’unité imprimés ; Lot n’est pas l’unité.')
        return
    if document_id not in {'jdoc-ecbb948261634b42a765e4a45fe406ba','jdoc-0cfedc54f94d495e83d1fc100261cdfb'}:
        return
    refs={int(LINE.search(f['path']).group(1)):f for f in fields if f['label']=='LINE_ITEM_REFERENCE' and LINE.search(f['path'])}
    for f in fields:
        m=LINE.search(f['path'])
        if not m or f['label']!='LINE_ITEM_DESCRIPTION' or f['status'] not in PENDING:
            continue
        ref=refs.get(int(m.group(1)))
        if not ref:
            continue
        candidates=[w for w in words if 8<w['bbox'][0]<125 and 260<w['bbox'][1]<740 and normalized(w['text'])==normalized(value(ref))]
        if len(candidates)!=1:
            continue
        a=candidates[0]
        main=selected(words,a['page'],130,450,a['bbox'][1]-2,a['bbox'][1]+6)
        if main and normalized(text(main))==normalized(value(f)):
            set_source(f,text(main),main,'Désignation sur une ligne, sous son en-tête graphique et alignée sur la référence.')
        elif main:
            f['comment']='La désignation continue sur des lignes séparées par une référence fabricant/remise. Le cadre unique actuel mélangerait ces champs ; annotation multi-zone ou arbitrage de la désignation à prévoir.'


def total_context_accepts(label,context):
    context=folded(context)
    if label=='TOTAL_GROSS':
        return bool(re.search(r'ttc|toutes taxes|total gross',context))
    if label=='TOTAL_AMOUNT_DUE':
        return bool(re.search(r'net\s+a\s+payer|montant\s+a\s+payer|amount due',context))
    if label=='TOTAL_VAT':
        return bool(re.search(r'\btva\b|vat|tax amount',context))
    if label=='TOTAL_SUBTOTAL':
        return bool(re.search(r'total\s+lignes|sous.total|subtotal|total\s+marchandises',context))
    if label=='TOTAL_NET':
        return bool(re.search(r'total\s+(?:net\s+)?h\.?\s*t|net\s+h\.?\s*t|hors taxe',context))
    return False


def semantic_context_accepts(pdf, words, field, candidate):
    """A matching string is not enough to establish its role or numeric value."""
    label, evidence = field['label'], candidate['evidence']
    source = candidate['text']
    numeric = label.startswith('TOTAL_') or label in {
        'LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE','LINE_ITEM_TOTAL','ADDITIONAL_CHARGE_AMOUNT'}
    if numeric and (decimal_value(source) is None or decimal_value(source) != decimal_value(value(field))):
        return False
    # Tax IDs must be complete occurrences, not the first nine digits of a
    # SIRET or a substring of another identifier.
    if label.endswith(('_SIREN','_SIRET','_VAT_NUMBER','_COUNTRY_CODE')):
        if normalized(source) != normalized(value(field)):
            return False
    if label.startswith(('BUYER_','SUPPLIER_')):
        x0,y0,x1,_ = evidence['bbox']
        headings = rows(selected(words,evidence['page'],max(0,x0-100),min(1000,x1+80),max(0,y0-100),y0+2))
        pattern = r'^(?:acheteur|emetteur|donneur d.ordre)\b' if label.startswith('BUYER_') else r'^(?:fournisseur|destinataire)\b'
        explicit = any(re.match(pattern,folded(text(line))) for line in headings)
        # Legal footer is an independent issuer occurrence, but not a supplier
        # address. Other unlabelled letterheads require visual/family review.
        issuer_footer = label.startswith('BUYER_') and y0 >= 880
        if not explicit and not issuer_footer:
            return False
    nearby = folded(nearby_text(pdf,evidence,horizontal=250,vertical=12))
    if label == 'DELIVERY_DATE' and not re.search(r'livraison|livrable|delivery',nearby):
        return False
    if label == 'OFFER_NUMBER' and not re.search(r'offre|devis|quotation',nearby):
        return False
    if label == 'DEROGATION_NUMBER' and not re.search(r'derogation',nearby):
        return False
    # Freight and eco-fees often occupy subrows with a unit price AND amount;
    # the general locator cannot establish those columns safely.
    if label.startswith('ADDITIONAL_CHARGE_'):
        return False
    return True


def apply_visual_case(document_id,fields,events):
    from scripts.studio_review_visual_cases import VISUAL_CASES
    case=VISUAL_CASES.get(document_id)
    if not case:
        return
    by_label={f['label']:f for f in fields}
    for label,(source,bounds) in case['fields'].items():
        field=by_label.get(label)
        if field is None and label.endswith('_STREET'):
            role=label[:-7].lower()
            field={'label':label,'path':f'order.addresses.{role}.selected.components.street','status':'UNREVIEWED','extracted_value':None}
            fields.append(field)
        if field:
            set_source(field,source,{'page':case.get('pages',{}).get(label,1),'bbox':bounds},'Valeur et rôle lus directement sur le rendu de la page PDF.',force=True)
    for label,reason in case.get('absent',{}).items():
        if label in by_label:
            absent(by_label[label],reason)
    for label,(source,bounds,path) in case.get('new',{}).items():
        field=by_label.get(label)
        if field is None:
            field={'label':label,'path':path,'status':'UNREVIEWED','extracted_value':None}
            fields.append(field)
        set_source(field,source,{'page':1,'bbox':bounds},'Champ manquant ajouté après lecture visuelle de la page.',force=True)
    full_roles={f['label'][:-7] for f in fields if f['label'].endswith('_STREET') and f['status'] in ACTIVE}
    for f in list(fields):
        if any(f['label']==role+'_'+suffix for role in full_roles for suffix in ATOMIC):
            fields.remove(f)
            role=next(role for role in full_roles if f['label'].startswith(role+'_'))
            events.append({'kind':'removed_street_component','field':f,'replacement_path':f'order.addresses.{role.lower()}.selected.components.street'})
    if case.get('rows') and not any(LINE.search(f['path']) for f in fields):
        for i,(ref,desc,qty,price,y) in enumerate(case['rows']):
            for label,suffix,source,bounds in [
                ('LINE_ITEM_REFERENCE','references.article_number',ref,[45,y,164,y+11]),
                ('LINE_ITEM_DESCRIPTION','description',desc,[202,y,581,y+11]),
                ('LINE_ITEM_QUANTITY','quantity',str(qty),[675,y,702,y+11]),
                ('LINE_ITEM_UNIT_PRICE','pricing.unit_price',price,[808,y,859,y+11]),
            ]:
                f={'label':label,'path':f'order.line_items[{i}].{suffix}','status':'UNREVIEWED','extracted_value':None,'compartment':'LINE_ITEMS'}
                set_source(f,source,{'page':1,'bbox':bounds},'Ligne manquante ajoutée depuis le tableau lu visuellement.',force=True)
                if label=='LINE_ITEM_DESCRIPTION' and case.get('clipped_descriptions'):
                    f['status']='AMBIGUOUS'
                    f['comment']='La désignation est tronquée à droite dans le PDF lui-même ; seul le texte visible est transcrit. Il faut une source complète pour connaître la fin.'
                fields.append(f)
            for label,suffix in [('LINE_ITEM_NUMBER','line_number'),('LINE_ITEM_TOTAL','amounts.net')]:
                f={'label':label,'path':f'order.line_items[{i}].{suffix}','status':'UNREVIEWED','extracted_value':None,'compartment':'LINE_ITEMS'}
                absent(f,'Cette colonne n’est pas imprimée sur le tableau ; ne pas annoter une valeur calculée.')
                fields.append(f)


def generic_review(fields,pdf,words,ocr,context):
    review_native_tables(fields,pdf,words)
    eligible=[f for f in fields if f['status'] in PENDING]
    keys=set().union(*(lookup_keys(f) for f in eligible)) if eligible else set()
    index=indexed_occurrences(pdf,keys)
    for f in eligible:
        label=f['label']
        if label in {'DOCUMENT_TYPE','LINE_ITEM_NUMBER'}:
            continue
        candidates={}
        for key in lookup_keys(f):
            for c in index.get(key,[]):
                e=c['evidence']
                candidates[(e['page'],tuple(e['bbox']))]=c
        accepted=[]
        for c in candidates.values():
            if not semantic_context_accepts(pdf,words,f,c):
                continue
            if label!='LINE_ITEM_REFERENCE' and not value_matches(label,value(f),c['text']):
                continue
            if label.startswith('TOTAL_') and not total_context_accepts(label,nearby_text(pdf,c['evidence'],horizontal=300,vertical=4)):
                continue
            if context_accepts(pdf,context,fields,f,c):
                accepted.append(c)
        if len(accepted)==1:
            c=accepted[0]
            # Generic locator can strip too much from long descriptions: require exact.
            if label=='LINE_ITEM_DESCRIPTION' and normalized(value(f))!=normalized(c['text']):
                continue
            source=c['text'] if label=='LINE_ITEM_REFERENCE' else value(f)
            set_source(f,source,c['evidence'],'Valeur source unique, contrôlée dans sa zone métier et sa ligne/colonne.')
    # Native or OCR title, not an occurrence buried in legal boilerplate.
    for f in fields:
        if f['label']!='DOCUMENT_TYPE' or f['status'] not in PENDING:
            continue
        titles=[]
        for line in rows(words+ocr):
            if line[0]['page']!=1 or line[0]['bbox'][1]>350 or min(w.get('confidence',100) for w in line)<85:
                continue
            title=folded(text(line))
            kind=None
            if re.fullmatch(r'(?:bon de )?commande(?: fournisseur| client)?(?:\s+n[°o].*)?',title):
                kind='purchase_order'
            elif re.match(r'confirmation de commande|accuse de reception de commande',title):
                kind='order_confirmation'
            elif re.match(r'(?:offre (?:de prix|commerciale)|devis)\b',title):
                kind='quotation'
            elif re.match(r'demande de prix\b',title):
                kind='request_for_quotation'
            if kind:
                titles.append((kind,line))
        if len({kind for kind,line in titles})==1:
            kind,line=titles[0]
            set_source(f,kind,line,'Type déterminé par le titre explicite du document.')


def rollback_new_conflicts(before,fields,events):
    baseline={f['path']:f for f in before}
    original=Counter(i['code'] for i in training_annotation_issues(before))
    for _ in range(len(fields)):
        seen=Counter()
        issue=None
        for candidate in training_annotation_issues(fields):
            seen[candidate['code']]+=1
            if seen[candidate['code']]>original[candidate['code']]:
                issue=candidate
                break
        if issue is None:
            return
        indexes=[issue['fieldIndex']]+([issue['otherFieldIndex']] if 'otherFieldIndex' in issue else [])
        changed=[i for i in indexes if fields[i]!=baseline.get(fields[i]['path'])]
        if not changed:
            return
        for i in changed:
            f=fields[i]
            f['status']='AMBIGUOUS'
            f['comment']='La preuve trouvée entre en conflit avec un autre champ : '+issue['message']
            events.append({'kind':'conflict_retained','path':f['path'],'issue':issue})


def plan_document(doc):
    before=doc['latestAnnotation']['field_annotations']
    fields=deepcopy(before)
    events=[]
    path=Path(doc['pdf_path'])
    if os.name=='nt':
        path=Path(str(path).replace('\\','/').removeprefix('/app/'))
    ocr_path=ROOT/'ocr'/(doc['documentId']+'.json')
    ocr_data=json.loads(ocr_path.read_text(encoding='utf-8')) if ocr_path.exists() else {'words':[],'errors':[{'error':'OCR pending'}]}
    with fitz.open(path) as pdf:
        # All evidence uses the displayed page coordinate system. MuPDF text
        # APIs otherwise return unrotated coordinates on landscape scans/PDFs.
        # This changes only the in-memory review copy, never the source file.
        for page in pdf:
            if page.rotation:
                page.remove_rotation()
        words=native_words(pdf)
        context=page_context(pdf)
        try:
            family=review_iserba(fields,words,ocr_data['words'],events)
        except ValueError as error:
            fields=deepcopy(before)
            events.append({'kind':'family_review_blocked','reason':str(error)})
            family=False
        generic_review(fields,pdf,words,ocr_data['words'],context)
        review_inspected_native_layouts(doc['documentId'],fields,pdf,words,events)
        street_merges(fields,words+ocr_data['words'],events)
        deduplicate_explicit_roles(fields,words+ocr_data['words'],context,events)
        apply_visual_case(doc['documentId'],fields,events)
        from scripts.studio_review_native_cases import review_case
        review_case(doc['documentId'],fields,words,events)
        rollback_new_conflicts(before,fields,events)
    changes=[]
    old={f['path']:f for f in before}
    for f in fields:
        if f!=old.get(f['path']):
            changes.append({'path':f['path'],'before':old.get(f['path']),'after':f})
    remaining=[{'label':f['label'],'path':f['path'],'value':value(f),'evidence':f.get('evidence'),'reason':f.get('comment')} for f in fields if f['status'] in PENDING]
    return {'document_id':doc['documentId'],'filename':doc['filename'],
        'base_digest':doc['latestAnnotation']['annotation_digest'],'base_revision':doc['latestAnnotation']['revision'],
        'document_sha256':doc['pdfSha256'],
        'ambiguous_before':sum(f['status']=='AMBIGUOUS' for f in before),'ambiguous_after':sum(f['status']=='AMBIGUOUS' for f in fields),
        'family':'iserba' if family else 'generic','ocr_errors':ocr_data['errors'],
        'changes':changes,'events':events,'remaining':remaining,'field_annotations':fields}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--document-id')
    args=parser.parse_args()
    if args.apply and os.name=='nt':
        parser.error('Apply only inside Linux sharing the Studio database')
    plans_dir=ROOT/'plans'
    plans_dir.mkdir(exist_ok=True)
    results=[]
    if args.apply:
        from jin_runtime.studio_store import StudioStore, StudioConflictError
        from datetime import datetime, timezone
        import sqlite3
        audits={a['document_id']:a for a in json.loads((ROOT/'evidence-audit.json').read_text(encoding='utf-8'))}
        # Complete preflight before any database changes.
        for path in sorted(plans_dir.glob('jdoc-*.json')):
            plan=json.loads(path.read_text(encoding='utf-8'))
            if not args.document_id or plan['document_id']==args.document_id:
                assert_audited(plan,audits.get(plan['document_id'],{}))
        store=StudioStore()
        backup_dir=store.root/'backups'
        backup_dir.mkdir(exist_ok=True)
        backup=backup_dir/('studio-pre-reviewed-apply-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
        with store._connect() as connection, sqlite3.connect(backup) as destination:
            connection.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Backup integrity check failed')
        print('Backup:',backup,flush=True)
        for path in sorted(plans_dir.glob('jdoc-*.json')):
            plan=json.loads(path.read_text(encoding='utf-8'))
            if args.document_id and plan['document_id']!=args.document_id:
                continue
            current=store.get_document(plan['document_id'])
            ann=current['latestAnnotation']
            if current['status']!='DRAFT' or ann['annotation_digest']!=plan['base_digest']:
                results.append({'document_id':plan['document_id'],'status':'SKIPPED_CHANGED_SINCE_PLAN'})
                continue
            if sha256(store.pdf_path(plan['document_id']).read_bytes()).hexdigest()!=plan['document_sha256']:
                results.append({'document_id':plan['document_id'],'status':'SKIPPED_SOURCE_CHANGED'})
                continue
            if not plan['changes'] and not any(e['kind'].startswith('removed_') for e in plan['events']):
                results.append({'document_id':plan['document_id'],'status':'UNCHANGED'})
                continue
            try:
                saved=store.save_annotation(plan['document_id'],{
                    'status':'DRAFT','reviewed_by':'jin-complete-annotation-review','field_annotations':plan['field_annotations'],
                    'note':'Relecture assistée sur toutes les pages ; preuves natives/OCR, correction des rôles et voies complètes. Relecture humaine requise avant apprentissage.'},
                    expected_digest=plan['base_digest'], expected_revision=plan['base_revision'])
            except StudioConflictError:
                results.append({'document_id':plan['document_id'],'status':'SKIPPED_CHANGED_SINCE_PLAN'})
                continue
            results.append({'document_id':plan['document_id'],'status':'SAVED','revision':saved['revision']})
            (ROOT/'applied.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            print(len(results),plan['filename'],flush=True)
        (ROOT/'applied.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        mismatches=[]
        for result in results:
            if result['status']!='SAVED':
                continue
            plan=json.loads((plans_dir/(result['document_id']+'.json')).read_text(encoding='utf-8'))
            actual=store.get_document(result['document_id'])['latestAnnotation']
            expected=normalize_studio_annotation({'status':'DRAFT','reviewed_by':'qa','field_annotations':plan['field_annotations']},
                document_id=result['document_id'],document_sha256=plan['document_sha256'],revision=result['revision'])
            if actual['status']!='DRAFT' or actual['field_annotations']!=expected['field_annotations']:
                mismatches.append(result['document_id'])
        current_documents=store.list_documents(limit=10000)
        verification={'backup':str(backup),'results':dict(Counter(r['status'] for r in results)),
            'readback_mismatches':mismatches,'document_statuses':dict(Counter(d['status'] for d in current_documents)),
            'field_statuses':dict(Counter(f['status'] for d in current_documents for f in (d.get('latestAnnotation') or {}).get('field_annotations',[])))}
        (ROOT/'verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(verification,ensure_ascii=False),flush=True)
        if mismatches:
            raise RuntimeError('Saved annotations changed or do not match plan; see verification.json')
    else:
        for path in sorted(ROOT.glob('jdoc-*.json')):
            doc=json.loads(path.read_text(encoding='utf-8'))
            if args.document_id and doc['documentId']!=args.document_id:
                continue
            if doc.get('status')!='DRAFT' or not any(f['status'] in PENDING for f in doc['latestAnnotation']['field_annotations']):
                continue
            plan=plan_document(doc)
            payload={'status':'DRAFT','reviewed_by':'jin-complete-annotation-review','field_annotations':plan['field_annotations']}
            normalize_studio_annotation(payload,document_id=doc['documentId'],document_sha256=doc['pdfSha256'],revision=doc['latestAnnotation']['revision']+1)
            (plans_dir/(doc['documentId']+'.json')).write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
            results.append({k:plan[k] for k in ('document_id','filename','ambiguous_before','ambiguous_after','family','ocr_errors')})
            print(len(results),plan['filename'],plan['ambiguous_before'],'->',plan['ambiguous_after'],flush=True)
        (ROOT/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print('TOTAL',sum(r['ambiguous_before'] for r in results),'->',sum(r['ambiguous_after'] for r in results),flush=True)


if __name__=='__main__':
    main()
