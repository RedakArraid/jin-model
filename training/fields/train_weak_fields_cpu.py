from __future__ import annotations
import argparse, re, json, math, time, hashlib
from pathlib import Path
from collections import defaultdict, Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import fitz
import numpy as np
import joblib
from scipy import sparse
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import classification_report, accuracy_score

STREET_TYPES={
 'RUE','AVENUE','AV','BOULEVARD','BD','ROUTE','RTE','CHEMIN','CHE','ALLEE','ALLÉE','IMPASSE','PLACE','QUAI','COURS','PASSAGE','SQUARE','VOIE','ROND-POINT','RONDPOINT','MONTEE','MONTÉE','TRAVERSE','RESIDENCE','RÉSIDENCE'
}
BAD_POSTAL_CONTEXT={'ARTICLE','ART','REFERENCE','RÉFÉRENCE','CODE','QTE','QTÉ','QUANTITE','QUANTITÉ','PRIX','MONTANT','PU','TOTAL','TVA','HT','TTC','TEL','TÉL','FAX'}
DATE_RE=re.compile(r'^(?:0?[1-9]|[12]\d|3[01])[/.\-](?:0?[1-9]|1[0-2])[/.\-](?:20\d{2}|\d{2})$')
MONEY_RE=re.compile(r'^[-+]?\d{1,3}(?:[ .]\d{3})*(?:[,.]\d{2,4})?$|^[-+]?\d+[,.]\d{2,4}$')
ORDER_HINTS=('COMMANDE','CMD','ORDER','CDE')

def norm(s):
    import unicodedata
    s=''.join(c for c in unicodedata.normalize('NFKD',str(s or '')) if not unicodedata.combining(c)).upper()
    return re.sub(r'[^A-Z0-9]+','',s)

def shape(s):
    out=[]
    last=None
    for c in s:
        m='d' if c.isdigit() else 'X' if c.isupper() else 'x' if c.islower() else c
        if m!=last: out.append(m); last=m
    return ''.join(out)[:16]

def words_by_line(doc):
    rows=[]
    for pi,page in enumerate(doc):
        if pi > 0:
            break
        rect=page.rect
        groups=defaultdict(list)
        native_words = page.get_text('words', sort=True)
        for raw in native_words:
            x0,y0,x1,y1,text,block,line,word=raw
            groups[(int(block),int(line))].append({
                'text':str(text),'n':norm(text),'bbox':(float(x0),float(y0),float(x1),float(y1)),
                'page':pi,'pw':float(rect.width),'ph':float(rect.height),'block':int(block),'line':int(line),'word':int(word)
            })
        for _, toks in sorted(groups.items(), key=lambda kv:(min(t['bbox'][1] for t in kv[1]),min(t['bbox'][0] for t in kv[1]))):
            toks.sort(key=lambda t:t['bbox'][0])
            rows.append(toks)
    return rows

def label_line(toks):
    labels=['O']*len(toks)
    ns=[t['n'] for t in toks]
    texts=[t['text'] for t in toks]
    line_norm=' '.join(ns)
    # BP / CS / TSA and values
    for i,n in enumerate(ns[:-1]):
        if n in {'BP','CS','TSA'} and re.fullmatch(r'\d{3,6}',ns[i+1]):
            labels[i]=f'ADDRESS_{n}'
            labels[i+1]=f'ADDRESS_{n}'
    # CEDEX
    for i,n in enumerate(ns):
        if n=='CEDEX':
            labels[i]='ADDRESS_CEDEX'
            if i+1<len(ns) and re.fullmatch(r'\d{1,3}',ns[i+1]): labels[i+1]='ADDRESS_CEDEX'
    # Street: number + type + name, high precision
    for i,n in enumerate(ns):
        if n in {norm(x) for x in STREET_TYPES}:
            # street type must have alpha name after it
            if i+1>=len(ns) or not any(c.isalpha() for c in texts[i+1]):
                continue
            labels[i]='ADDRESS_STREET_TYPE'
            # house number immediately before, allow suffix token
            if i>0 and re.fullmatch(r'\d{1,4}(?:[-/]\d{1,4})?',ns[i-1]): labels[i-1]='ADDRESS_HOUSE_NUMBER'
            elif i>1 and ns[i-1] in {'BIS','TER','QUATER'} and re.fullmatch(r'\d{1,4}',ns[i-2]):
                labels[i-2]='ADDRESS_HOUSE_NUMBER'; labels[i-1]='ADDRESS_HOUSE_NUMBER_SUFFIX'
            # label name to EOL, stop obvious phone/email/country/postal markers
            for j in range(i+1,len(ns)):
                if ns[j] in {'FRANCE','TEL','TELEPHONE','FAX','EMAIL','MAIL','BP','CS','TSA'}: break
                if re.fullmatch(r'\d{5}',ns[j]): break
                if '@' in texts[j]: break
                labels[j]='ADDRESS_STREET_NAME'
            break
    # postal code + city (avoid product/table lines)
    bad=any(x in BAD_POSTAL_CONTEXT for x in ns)
    if not bad:
        for i,n in enumerate(ns):
            if re.fullmatch(r'\d{5}',n) and 1000 <= int(n) <= 98999:
                # require at least one alphabetic city token after it, no money punctuation
                city=[]
                for j in range(i+1,min(len(ns),i+7)):
                    if ns[j]=='CEDEX': break
                    if re.fullmatch(r'\d+',ns[j]): break
                    if any(c.isalpha() for c in texts[j]) and '@' not in texts[j]: city.append(j)
                    else: break
                if city:
                    labels[i]='ADDRESS_POSTAL_CODE'
                    for j in city: labels[j]='ADDRESS_CITY'
                    # CEDEX handled separately
                    break
    # Order number
    if any(h in line_norm for h in ORDER_HINTS):
        for i,n in enumerate(ns):
            if not n or DATE_RE.match(texts[i]) or n in {'COMMANDE','FOURNISSEUR','CLIENT','NUMERO','NO','N','CDE','CMD','ORDER'}: continue
            # after a commande token or colon-ish context, code has alpha+digit or >=6 digits
            left=' '.join(ns[max(0,i-4):i])
            if any(h in left for h in ORDER_HINTS) and (len(n)>=6 and (any(c.isdigit() for c in n))):
                if not re.fullmatch(r'\d{10,}',n):
                    labels[i]='ORDER_NUMBER'; break
    # Date lines
    if 'DATE' in line_norm or any(h in line_norm for h in ORDER_HINTS):
        for i,t in enumerate(texts):
            if DATE_RE.match(t.strip()): labels[i]='ORDER_DATE'; break
    # Totals: amount at end of total lines
    target=None
    if 'TOTALTTC' in ''.join(ns) or 'MONTANTTTC' in ''.join(ns) or 'NETAPAYER' in ''.join(ns): target='TOTAL_GROSS'
    elif 'TOTALHT' in ''.join(ns) or 'SOUS TOTAL' in line_norm or 'SOUS TOTAL' in ' '.join(texts).upper(): target='TOTAL_NET'
    elif 'TVA' in ns or any(n.startswith('TVA') for n in ns): target='TOTAL_VAT'
    if target:
        for i in range(len(texts)-1,-1,-1):
            v=texts[i].replace('€','').replace('EUR','').strip()
            if MONEY_RE.match(v) and any(c.isdigit() for c in v): labels[i]=target; break
    return labels

def context(toks, labels, idx):
    t=toks[idx]; pw=max(t['pw'],1); ph=max(t['ph'],1)
    x0,y0,x1,y1=t['bbox']; cx=(x0+x1)/2/pw; cy=(y0+y1)/2/ph
    def at(j): return '<BOS>' if j<0 else '<EOS>' if j>=len(toks) else toks[j]['n']
    return f"p2={at(idx-2)} p1={at(idx-1)} tok={t['n']} n1={at(idx+1)} n2={at(idx+2)} shape={shape(t['text'])} xb={min(9,int(cx*10))} yb={min(9,int(cy*10))} len={min(20,len(t['text']))} page={min(5,t['page'])}"

def process_doc(args):
    split,pstr=args; p=Path(pstr)
    try: doc=fitz.open(p)
    except Exception: return [],{},False
    lines=words_by_line(doc); doc.close()
    if not lines: return [],{},False
    samples=[]; counts=Counter(); doc_has=False
    for toks in lines:
        labels=label_line(toks)
        line_has=any(l!='O' for l in labels)
        if line_has: doc_has=True
        for i,l in enumerate(labels):
            # strong positives + all tokens from positive lines + sparse negatives elsewhere
            if l!='O' or line_has or (int(hashlib.sha1(f"{p.name}|{toks[0]['block']}|{toks[0]['line']}|{i}".encode()).hexdigest()[:8],16) % 32 == 0):
                samples.append((context(toks,labels,i),l)); counts[l]+=1
    return samples,dict(counts),doc_has

def extract_split(root, split):
    X=[]; y=[]; docs=0; native=0; counts=Counter(); labeled_docs=0
    paths=[p for p in sorted((root/split).glob('*')) if p.suffix.lower()=='.pdf']
    docs=len(paths)
    with ProcessPoolExecutor(max_workers=5) as pool:
        futs=[pool.submit(process_doc,(split,str(p))) for p in paths]
        for done,f in enumerate(as_completed(futs),1):
            samples,c,has=f.result()
            if samples: native+=1
            if has: labeled_docs+=1
            counts.update(c)
            X.extend(a for a,b in samples); y.extend(b for a,b in samples)
            if done%250==0: print(split,done,'docs samples',len(X),'labels',dict(counts),flush=True)
    return X,np.array(y,dtype=object),{'docs':docs,'native_docs':native,'labeled_docs':labeled_docs,'label_counts':dict(counts)}

def main():
    parser=argparse.ArgumentParser(description="Train the JIN CPU weak-supervised first-page field router.")
    parser.add_argument("--corpus-root", required=True, help="Directory containing test/ and validation/ real PDFs")
    parser.add_argument("--output", default="data/learning/jin-field-weak-router-v2-cpu.joblib")
    parser.add_argument("--metrics", default="data/learning/jin-field-weak-router-v2-cpu-metrics.json")
    args=parser.parse_args()
    root=Path(args.corpus_root); out=Path(args.output); met=Path(args.metrics)
    t=time.time(); Xtr,ytr,st=extract_split(root,'test'); Xv,yv,sv=extract_split(root,'validation')
    vec=HashingVectorizer(analyzer='char_wb',ngram_range=(2,5),n_features=2**15,alternate_sign=False,norm='l2',lowercase=True)
    model=SGDClassifier(loss='log_loss',alpha=2e-6,max_iter=3000,tol=1e-4,class_weight='balanced',random_state=42,average=True)
    model.fit(vec.transform(Xtr),ytr)
    pred=model.predict(vec.transform(Xv))
    report=classification_report(yv,pred,output_dict=True,zero_division=0)
    labels=[x for x in sorted(set(yv)) if x!='O']; field={k:report.get(k,{}) for k in labels}
    metrics={
      'version':'jin-field-weak-router-v2-cpu','weak_supervision':True,
      'train':st,'validation':sv,'validation_token_accuracy':float(accuracy_score(yv,pred)),
      'validation_macro_f1_all':float(report['macro avg']['f1-score']),
      'field_metrics':field,'elapsed_seconds':time.time()-t,
      'qualification':'Agreement against deterministic weak labels only; not ground-truth field accuracy.'
    }
    bundle={'version':'jin-field-weak-router-v2-cpu','n_features':2**15,'model':model,'metrics':metrics,'qualification':metrics['qualification']}
    out.parent.mkdir(parents=True,exist_ok=True); met.parent.mkdir(parents=True,exist_ok=True)
    joblib.dump(bundle,out,compress=3); met.write_text(json.dumps(metrics,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'output':str(out),'metrics':str(met),'validation_token_accuracy':metrics['validation_token_accuracy'],'validation_macro_f1_all':metrics['validation_macro_f1_all']},indent=2,ensure_ascii=False))

if __name__=='__main__': main()
