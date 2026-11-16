"""Independent campaign accounting, geometry checks, and a local review queue."""
from collections import Counter
import html
import json
from pathlib import Path

import fitz

from jin_runtime.studio_contract import training_annotation_issues, normalize_studio_annotation
from scripts.studio_annotation_pdf_audit import value_matches
from scripts.studio_complete_ambiguity_review import ROOT, ACTIVE, value, plan_digest
from scripts.studio_table_ambiguity_review import decimal_value


def numeric_label(label):
    return label.startswith('TOTAL_') or label in {
        'LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE','LINE_ITEM_TOTAL','ADDITIONAL_CHARGE_AMOUNT'}


def source_matches(field, printed):
    if numeric_label(field['label']):
        return decimal_value(printed)==decimal_value(value(field)) and decimal_value(printed) is not None
    if field['label']=='DOCUMENT_TYPE':
        return bool(printed.strip())  # Semantic title review is recorded separately.
    return value_matches(field['label'],value(field),printed)


def complete_words_in_box(pdf, evidence, word_cache=None):
    """Exclude clipped commas/descenders from an adjacent text line.

    MuPDF's text clip can include a fragment of an adjacent glyph. Checking
    word centers avoids interpreting that fragment as a second numeric value.
    """
    page=pdf[evidence['page']-1]
    a,b,c,d=evidence['bbox']
    selected=[]
    if word_cache is None:
        page_words=page.get_text('words')
    else:
        if page.number not in word_cache:
            word_cache[page.number]=page.get_text('words')
        page_words=word_cache[page.number]
    for word in page_words:
        displayed=fitz.Rect(word[:4])*page.rotation_matrix
        x=(displayed.x0+displayed.x1)/2/page.rect.width*1000
        y=(displayed.y0+displayed.y1)/2/page.rect.height*1000
        if a<=x<=c and b<=y<=d:
            selected.append(word)
    # Some PDFs contain an exactly duplicated text layer; don't turn RODEZ
    # into RODEZ RODEZ when it is painted twice at the same coordinates.
    unique={(tuple(round(v,2) for v in w[:4]),w[4]):w for w in selected}
    return ' '.join(w[4] for w in sorted(unique.values(),key=lambda w:(w[5],w[6],w[7])))


def category(field):
    reason=(field.get('reason') or field.get('comment') or '').lower()
    if 'scinder' in reason or 'plusieurs commandes' in reason:
        return 'PDF à scinder'
    if 'tronqu' in reason:
        return 'Texte tronqué dans la source'
    if 'multi-zone' in reason:
        return 'Désignation sur plusieurs zones'
    if 'conflit' in reason:
        return 'Conflit de preuves'
    if field['label'].startswith('LINE_ITEM_'):
        return 'Ligne de commande à relire'
    if field['label'].startswith(('BUYER_','SUPPLIER_','SHIP_TO_','BILL_TO_')):
        return 'Valeur ou rôle à vérifier'
    return 'Valeur à vérifier dans le PDF'


def analyze_plan(plan,doc):
    before=doc['latestAnnotation']['field_annotations']
    after=plan['field_annotations']
    old={f['path']:f for f in before}
    new={f['path']:f for f in after}
    transitions=Counter()
    for path,f in old.items():
        if f['status']!='AMBIGUOUS':
            continue
        transitions[new[path]['status'] if path in new else 'REMOVED_WITH_AUDIT']+=1
    newly_ambiguous=sum(f['status']=='AMBIGUOUS' and old.get(path,{}).get('status')!='AMBIGUOUS' for path,f in new.items())
    baseline=Counter(i['code'] for i in training_annotation_issues(before))
    final=Counter(i['code'] for i in training_annotation_issues(after))
    increased=final-baseline
    normalize_studio_annotation({'status':'DRAFT','reviewed_by':'qa','field_annotations':after},
        document_id=doc['documentId'],document_sha256=doc['pdfSha256'],revision=plan['base_revision']+1)
    geometry=[]
    unsupported=[]
    raster=[]
    pdf_path=Path('data/studio/documents')/doc['documentId']/'source.pdf'
    with fitz.open(pdf_path) as pdf:
        word_cache={}
        for f in after:
            if f['status'] not in ACTIVE or f==old.get(f['path']):
                continue
            e=f.get('evidence') or {}
            b=e.get('bbox') or []
            valid=len(b)==4 and 1<=int(e.get('page',0))<=len(pdf) and 0<=b[0]<b[2]<=1000 and 0<=b[1]<b[3]<=1000
            if not valid:
                geometry.append(f['path'])
                continue
            printed=complete_words_in_box(pdf,e,word_cache)
            if not printed:
                raster.append(f['path'])
            elif not source_matches(f,printed):
                unsupported.append({'path':f['path'],'label':f['label'],'value':value(f),'printed':printed,'evidence':e})
    return {'document_id':doc['documentId'],'plan_digest':plan_digest(plan),'transitions':dict(transitions),'new_ambiguities':newly_ambiguous,
            'increased_training_issues':dict(increased),'invalid_geometry':geometry,
            'native_evidence_disagreements':unsupported,'raster_evidence':raster}


def main():
    plans=[json.loads(p.read_text(encoding='utf-8')) for p in sorted((ROOT/'plans').glob('jdoc-*.json'))]
    counts=Counter()
    transitions=Counter()
    residual_categories=Counter()
    audits=[]
    sections=[]
    for plan in plans:
        doc=json.loads((ROOT/(plan['document_id']+'.json')).read_text(encoding='utf-8'))
        audit=analyze_plan(plan,doc)
        audits.append(audit)
        transitions.update(audit['transitions'])
        counts['new_ambiguities']+=audit['new_ambiguities']
        counts['ambiguous_before']+=plan['ambiguous_before']
        counts['ambiguous_after']+=plan['ambiguous_after']
        counts['documents_with_no_ambiguity']+=not plan['ambiguous_after']
        counts['increased_training_issues']+=sum(audit['increased_training_issues'].values())
        counts['invalid_geometry']+=len(audit['invalid_geometry'])
        counts['native_evidence_disagreements']+=len(audit['native_evidence_disagreements'])
        counts['raster_evidence']+=len(audit['raster_evidence'])
        remaining=plan['remaining']
        rows=[]
        for f in remaining:
            c=category(f)
            residual_categories[c]+=1
            e=f.get('evidence') or {}
            page=e.get('page',1)
            source=f'../../studio/documents/{plan["document_id"]}/source.pdf#page={page}'
            rows.append('<tr>'+''.join('<td>'+html.escape(str(v or ''))+'</td>' for v in
                (c,f['label'],f['value'],f.get('reason'),f['path']))+
                f'<td><a href="{source}" target="_blank" rel="noopener">PDF p. {page}</a></td></tr>')
        if rows:
            sections.append(f'<details><summary>{html.escape(plan["filename"])} — {len(rows)} champs</summary>'+
                '<table><thead><tr><th>À faire</th><th>Champ</th><th>Valeur proposée</th><th>Motif</th><th>Chemin</th><th>Source</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></details>')
    counts['documents']=len(plans)
    # Exact accounting: removed duplicates are NOT counted as confirmed fields.
    assert counts['ambiguous_after']==transitions['AMBIGUOUS']+counts['new_ambiguities']
    summary={'counts':dict(counts),'ambiguous_field_transitions':dict(transitions),'remaining_categories':dict(residual_categories),
             'qualification':'Préannotations assistées, pas une validation humaine ni une mesure de précision. Les résidus incluent des cas encore à relire, pas seulement des sources illisibles.'}
    (ROOT/'review-report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'evidence-audit.json').write_text(json.dumps(audits,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'summary.json').write_text(json.dumps([{k:p[k] for k in ('document_id','filename','ambiguous_before','ambiguous_after','family','ocr_errors')} for p in plans],ensure_ascii=False,indent=2),encoding='utf-8')
    content='''<!doctype html><html lang="fr"><meta charset="utf-8"><title>Relecture des annotations JIN</title>
<style>body{font:15px system-ui;margin:2rem;color:#18263d}input{padding:.7rem;width:70%}summary{cursor:pointer;padding:1rem;background:#edf2fa;margin-top:.5rem}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:.6rem;border:1px solid #ddd;overflow-wrap:anywhere}td:nth-child(4){min-width:250px}th{background:#f7f7f7}</style>
<h1>File de relecture des annotations</h1><p>Les corrections restent des brouillons. Les valeurs affichées ci-dessous ne sont pas validées. Certains cadres sont encore à recaler.</p>'''
    content+=f'<p>{counts["documents"]} documents examinés · {counts["ambiguous_before"]} → {counts["ambiguous_after"]} ambiguïtés. '+html.escape(summary['qualification'])+'</p>'
    content+='<h2>Décompte des ambiguïtés initiales</h2><table><tr><th>Traitement</th><th>Champs</th></tr>'
    for key,label in [('CONFIRMED','Valeur confirmée par la source'),('CORRECTED','Valeur corrigée'),('ABSENT','Champ non imprimé, avec justification'),('REMOVED_WITH_AUDIT','Composant remplacé par une voie complète ou doublon retiré'),('AMBIGUOUS','Ambiguïté initiale encore ouverte')]:
        content+=f'<tr><td>{label}</td><td>{transitions[key]}</td></tr>'
    content+=f'</table><p>{counts["new_ambiguities"]} ambiguïtés supplémentaires ont été signalées pendant la relecture. Les retraits sont tracés dans les plans et ne sont pas comptés comme des valeurs confirmées.</p>'
    content+='<p>Aucune validation humaine ni aucun entraînement n’a été déclenché. Rafraîchir le document dans Studio pour charger sa dernière révision.</p>'
    previews=sorted((ROOT/'visual-review').glob('*-review-*.png'))
    if previews:
        content+='<details><summary>Exemples de contrôles visuels des cadres</summary><ul>'+''.join(
            f'<li><a href="visual-review/{html.escape(p.name)}">{html.escape(p.name)}</a></li>' for p in previews)+'</ul></details>'
    content+='<input id="filter" placeholder="Filtrer par client, champ, valeur ou motif" aria-label="Filtrer la relecture">'+''.join(sections)
    content+='''<script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLocaleLowerCase('fr');document.querySelectorAll('details').forEach(d=>{d.hidden=!d.textContent.toLocaleLowerCase('fr').includes(q)})})</script></html>'''
    (ROOT/'relecture.html').write_text(content,encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
