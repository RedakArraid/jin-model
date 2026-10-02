from __future__ import annotations
import copy, json, os, re, threading, unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jin_runtime.feedback_contract import (
    FEEDBACK_SCHEMA_VERSION,
    FeedbackValidationError,
    normalize_human_feedback,
)

try:
    import joblib
    from sklearn.feature_extraction.text import HashingVectorizer
    from sklearn.linear_model import SGDClassifier
except Exception:
    joblib = HashingVectorizer = SGDClassifier = None

MODEL_VERSION = "jin-statistical-address-memory-v2"
TOKEN_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
FIELDS = {"building","residence","entry","floor","unit","house_number","house_number_suffix","street_type","street_name","industrial_zone","activity_park","place_name","postal_box","tsa","cs","postal_routing_code","postal_code","city","cedex_number","district","insee_code","department","region","state","country","country_code"}
ALIASES = {"number":"house_number","numero":"house_number","suffix":"house_number_suffix","street_number_suffix":"house_number_suffix","street_kind":"street_type","voie_type":"street_type","street":"street_name","voie":"street_name","lieu_dit":"place_name","bp":"postal_box","zipcode":"postal_code","zip_code":"postal_code","commune":"city","ville":"city","code_insee":"insee_code","cedex_no":"cedex_number"}
SINGLE = {"house_number","house_number_suffix","floor","entry","unit","postal_box","tsa","cs","postal_routing_code","postal_code","cedex_number","insee_code","country_code"}
PARTY_ROLES = frozenset({
    "buyer", "ordering_party", "supplier", "sold_to", "bill_to", "ship_to",
    "deliver_to", "invoice_to", "payer", "end_customer", "consignee",
    "ship_from",
})


def normalize_text(v: Any) -> str:
    if v is None: return ""
    s = "".join(c for c in unicodedata.normalize("NFKD", str(v)) if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9'-]+", " ", s)).strip()


def tokenize(v: Any) -> list[str]: return TOKEN_RE.findall(str(v or ""))


def canonical_components(obj: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(obj, dict): return {}
    merged = dict(obj.get("address") or {}) if isinstance(obj.get("address"), dict) else {}
    merged.update({k:v for k,v in obj.items() if k != "address"})
    return {ALIASES.get(k,k):str(v).strip() for k,v in merged.items() if ALIASES.get(k,k) in FIELDS and v not in (None,"") and not isinstance(v,bool)}


def address_text(block: dict[str, Any] | None) -> str:
    if not isinstance(block, dict): return ""
    for k in ("formatted_address","canonical_label","raw_address","address_text"):
        if isinstance(block.get(k), str) and block[k].strip(): return block[k].strip()
    a = block.get("address") if isinstance(block.get("address"),dict) else block
    order = ("building","residence","entry","floor","unit","house_number","house_number_suffix","street_type","street_name","industrial_zone","activity_park","place_name","postal_box","tsa","cs","postal_routing_code","postal_code","city","cedex_number","country")
    return " ".join(str(a[k]) for k in order if a.get(k) not in (None,"",False))


def label_tokens_from_components(text: str, components: dict[str, Any]) -> tuple[list[str], list[str]]:
    tokens, labels = tokenize(text), []
    norm = [normalize_text(x) for x in tokens]; labels = ["O"]*len(tokens)
    seqs=[]
    for label,value in canonical_components(components).items():
        seq=[normalize_text(x) for x in tokenize(value)]
        if seq: seqs.append((len(seq),label,seq))
    for _,label,seq in sorted(seqs, reverse=True):
        n=len(seq)
        for i in range(len(norm)-n+1):
            if norm[i:i+n]==seq and all(x=="O" for x in labels[i:i+n]): labels[i:i+n]=[label]*n; break
    return tokens, labels


def _ctx(tokens:list[str], i:int)->str:
    def at(j): return "<BOS>" if j<0 else "<EOS>" if j>=len(tokens) else normalize_text(tokens[j])
    t=tokens[i]; shape=re.sub(r"[A-Z]+","X",re.sub(r"[a-z]+","x",re.sub(r"\d+","d",t)))
    return f"p1={at(i-1)} tok={normalize_text(t)} n1={at(i+1)} shape={shape[:20]} digit={int(any(c.isdigit() for c in t))}"


def _lists(obj:Any):
    if isinstance(obj,dict):
        for k,v in obj.items():
            if k in {"business_addresses","addresses"} and isinstance(v,list):
                items=[x for x in v if isinstance(x,dict)]
                if items: yield items
            yield from _lists(v)
    elif isinstance(obj,list):
        for v in obj: yield from _lists(v)


def _pairs(event:dict[str,Any]):
    original=next(iter(_lists(event.get("extraction") or {})),[])
    raw=event.get("corrections") or event.get("corrected") or {}
    corrected=raw if isinstance(raw,list) else ((raw.get("business_addresses") or raw.get("addresses") or []) if isinstance(raw,dict) else [])
    out=[]
    for pos,c in enumerate(x for x in corrected if isinstance(x,dict)):
        if "index" in c:
            idx=c.get("index")
            if isinstance(idx,bool) or not isinstance(idx,int):
                raise FeedbackValidationError("corrected address index must be an integer")
            if idx<0 or idx>=len(original):
                raise FeedbackValidationError("corrected address index is out of range")
            src=original[idx]
        else:
            src=original[pos] if pos<len(original) else {}
        role=c.get("role")
        if role not in (None,""):
            role=str(role).strip().lower()
            if role not in PARTY_ROLES:
                raise FeedbackValidationError("corrected address role is not canonical")
            c=dict(c);c["role"]=role
        text=address_text(c) or address_text(src) or " ".join(canonical_components(c).values())
        if text: out.append((text,src,c))
    return out


@dataclass
class LearningConfig:
    role_override_threshold:float=.93; component_fill_threshold:float=.88
    min_role_examples:int=4; min_token_examples:int=30
    allow_role_override:bool=True; fill_missing_components:bool=True
    @classmethod
    def from_env(cls):
        def f(n,d):
            try:return float(os.getenv(n,str(d)))
            except ValueError:return d
        def i(n,d):
            try:return int(os.getenv(n,str(d)))
            except ValueError:return d
        return cls(f("JIN_ROLE_OVERRIDE_THRESHOLD",.93),f("JIN_COMPONENT_FILL_THRESHOLD",.88),i("JIN_MIN_ROLE_EXAMPLES",4),i("JIN_MIN_TOKEN_EXAMPLES",30),os.getenv("JIN_ALLOW_ROLE_OVERRIDE","1").lower() not in {"0","false"},os.getenv("JIN_FILL_MISSING_COMPONENTS","1").lower() not in {"0","false"})


class StatisticalAddressLearner:
    def __init__(self, feedback_path=None, model_dir=None, config=None):
        self.feedback_path=Path(feedback_path or os.getenv("JIN_FEEDBACK_PATH","/app/training/feedback/learning_feedback.jsonl")); self.model_dir=Path(model_dir or os.getenv("JIN_LEARNING_MODEL_DIR","/app/data/learning")); self.model_path=self.model_dir/"address_models.joblib"; self.config=config or LearningConfig.from_env(); self._lock=threading.RLock(); self._bundle={}; self._load_reason=None; self._load()
    @property
    def available(self): return all(x is not None for x in (joblib,HashingVectorizer,SGDClassifier))
    def _vec(self): return HashingVectorizer(n_features=2**18,alternate_sign=False,analyzer="char_wb",ngram_range=(2,5),lowercase=True,norm="l2")
    def _load(self):
        if self.available and self.model_path.exists():
            try:
                loaded=joblib.load(self.model_path) or {}
                if loaded.get("feedback_schema_version")==FEEDBACK_SCHEMA_VERSION:
                    self._bundle=loaded
                else:
                    self._bundle={};self._load_reason="incompatible-feedback-schema"
            except Exception:
                self._bundle={};self._load_reason="model-load-error"
    def _read_events(self):
        if not self.feedback_path.exists(): return [],0,0
        out=[];total=0;malformed=0
        for line in self.feedback_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():continue
            total+=1
            try:x=json.loads(line)
            except (json.JSONDecodeError,RecursionError):
                malformed+=1;continue
            if isinstance(x,dict):out.append(x)
            else:malformed+=1
        return out,total,malformed
    def _events(self):
        return self._read_events()[0]
    def train(self):
        with self._lock:
            if not self.available:return self.status(reason="scikit-learn-unavailable")
            rx=[]; ry=[]; tx=[]; ty=[]; docs=0
            raw_events,total_records,malformed_records=self._read_events()
            events=[];event_pairs=[];seen_annotations={};duplicate_records=0
            for event in raw_events:
                try:
                    normalized=normalize_human_feedback(event)
                    pairs=_pairs(normalized)
                    if not pairs:raise FeedbackValidationError("feedback has no usable corrected address")
                    annotation_id=normalized["annotation_id"]
                    if annotation_id in seen_annotations:
                        duplicate_records+=1;continue
                    seen_annotations[annotation_id]=normalized["annotation_digest"]
                    events.append(normalized);event_pairs.append(pairs)
                except FeedbackValidationError:pass
            for pairs in event_pairs:
                docs+=1
                for text,src,c in pairs:
                    role=c.get("role")
                    if isinstance(role,str) and role.strip():rx.append(" ".join([text,str(c.get("party_name") or src.get("party_name") or ""),str(src.get("role") or "")]));ry.append(role.strip().lower())
                    if canonical_components(c):
                        toks,labs=label_tokens_from_components(text,c);tx.extend(_ctx(toks,j) for j in range(len(toks)));ty.extend(labs)
            vec=self._vec(); b={"model_version":MODEL_VERSION,"feedback_schema_version":FEEDBACK_SCHEMA_VERSION,"trained_at":datetime.now(timezone.utc).isoformat(),"events":len(events),"feedback_records":total_records,"rejected_feedback_records":total_records-len(events),"malformed_feedback_records":malformed_records,"duplicate_feedback_records":duplicate_records,"documents_with_labels":docs,"role_examples":len(rx),"token_examples":len(tx),"role_model":None,"component_model":None}
            if len(rx)>=self.config.min_role_examples and len(set(ry))>=2:
                m=SGDClassifier(loss="log_loss",alpha=1e-5,max_iter=2000,tol=1e-4,class_weight="balanced",random_state=42);m.fit(vec.transform(rx),ry);b["role_model"]=m
            if len(tx)>=self.config.min_token_examples and len(set(ty))>=2:
                m=SGDClassifier(loss="log_loss",alpha=1e-5,max_iter=2500,tol=1e-4,class_weight="balanced",random_state=42);m.fit(vec.transform(tx),ty);b["component_model"]=m
            self.model_dir.mkdir(parents=True,exist_ok=True);tmp_path=self.model_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                joblib.dump(b,tmp_path);os.replace(tmp_path,self.model_path)
            finally:
                tmp_path.unlink(missing_ok=True)
            self._bundle=b;self._load_reason=None;return self.status()
    def record_feedback(self,event,retrain=True):
        p=normalize_human_feedback(event)
        if not _pairs(p):raise FeedbackValidationError("feedback has no usable corrected address")
        p["recorded_at"]=datetime.now(timezone.utc).isoformat();self.feedback_path.parent.mkdir(parents=True,exist_ok=True)
        with self._lock:
            for existing in self._events():
                if str(existing.get("annotation_id") or "")!=p["annotation_id"]:continue
                try:existing=normalize_human_feedback(existing)
                except FeedbackValidationError as exc:
                    raise FeedbackValidationError(
                        "existing annotation with this id is invalid"
                    ) from exc
                if existing["annotation_digest"]==p["annotation_digest"]:
                    status=self.status();status.update(annotation_id=p["annotation_id"],feedback_recorded=False,feedback_duplicate=True);return status
                raise FeedbackValidationError("annotation_id already exists with different content")
            with self.feedback_path.open("a",encoding="utf-8") as h:
                h.write(json.dumps(p,ensure_ascii=False,allow_nan=False)+"\n");h.flush();os.fsync(h.fileno())
            status=self.train() if retrain else self.status()
            status.update(annotation_id=p["annotation_id"],feedback_recorded=True,feedback_duplicate=False,pending_retrain=not retrain);return status
    def predict_role(self,text):
        m=self._bundle.get("role_model")
        if m is None or not text.strip():return None
        p=m.predict_proba(self._vec().transform([text]))[0];j=int(p.argmax());return {"role":str(m.classes_[j]),"confidence":float(p[j])}
    def predict_components(self,text):
        m=self._bundle.get("component_model"); toks=tokenize(text)
        if m is None or not toks:return {}
        p=m.predict_proba(self._vec().transform([_ctx(toks,j) for j in range(len(toks))])); labs=m.classes_[p.argmax(axis=1)]; conf=p.max(axis=1);g={}
        for j,(tok,lab,cf) in enumerate(zip(toks,labs,conf)):
            lab=str(lab);cf=float(cf)
            if lab in FIELDS and cf>=self.config.component_fill_threshold:g.setdefault(lab,[]).append((j,tok,cf))
        out={}
        for lab,vals in g.items():
            if lab in SINGLE:_,value,cf=max(vals,key=lambda x:x[2])
            else:value=" ".join(x[1] for x in vals);cf=sum(x[2] for x in vals)/len(vals)
            out[lab]={"value":value,"confidence":float(cf)}
        return out
    def _enrich(self,b):
        text=address_text(b)
        if not text:return
        stat={"model_version":MODEL_VERSION};role=self.predict_role(" ".join([text,str(b.get("party_name") or ""),str(b.get("role") or b.get("role_label") or "")]))
        if role:
            stat["role"]=role;existing=b.get("role_confidence") if isinstance(b.get("role_confidence"),(int,float)) else (0.0 if not (b.get("role") or b.get("role_label")) else 1.0)
            if self.config.allow_role_override and role["confidence"]>=self.config.role_override_threshold and float(existing)<self.config.role_override_threshold:b.update(role=role["role"],role_confidence=role["confidence"],role_source="statistical_history")
        comps=self.predict_components(text)
        if comps:
            stat["components"]=comps;target=b.get("address") if isinstance(b.get("address"),dict) else b;cm=b.setdefault("component_confidence",{});sm=b.setdefault("component_source",{})
            if self.config.fill_missing_components:
                for k,pred in comps.items():
                    if target.get(k) in (None,""):target[k]=pred["value"];cm.setdefault(k,pred["confidence"]);sm[k]="statistical_history"
        if len(stat)>1:b["statistical_learning"]=stat
    def enrich(self,result):
        out=copy.deepcopy(result)
        for items in _lists(out):
            for b in items:self._enrich(b)
        out.setdefault("learning",{})["statistical_memory"]=self.status(compact=True);return out
    def status(self,reason=None,compact=False):
        b=self._bundle;d={"enabled":self.available,"model_version":MODEL_VERSION,"feedback_schema_version":FEEDBACK_SCHEMA_VERSION,"trained":bool(b.get("role_model") is not None or b.get("component_model") is not None),"role_model_trained":bool(b.get("role_model") is not None),"component_model_trained":bool(b.get("component_model") is not None),"events":int(b.get("events",0) or 0),"feedback_records":int(b.get("feedback_records",0) or 0),"rejected_feedback_records":int(b.get("rejected_feedback_records",0) or 0),"malformed_feedback_records":int(b.get("malformed_feedback_records",0) or 0),"duplicate_feedback_records":int(b.get("duplicate_feedback_records",0) or 0),"documents_with_labels":int(b.get("documents_with_labels",0) or 0),"role_examples":int(b.get("role_examples",0) or 0),"token_examples":int(b.get("token_examples",0) or 0),"trained_at":b.get("trained_at")}
        if self._load_reason and not reason:d["reason"]=self._load_reason
        if reason:d["reason"]=reason
        if not compact:
            _,records_on_disk,malformed_on_disk=self._read_events()
            d.update(feedback_path=str(self.feedback_path),model_path=str(self.model_path),feedback_records_on_disk=records_on_disk,malformed_feedback_records_on_disk=malformed_on_disk,pending_feedback_records=max(0,records_on_disk-d["feedback_records"]),guardrails={"role_override_threshold":self.config.role_override_threshold,"component_fill_threshold":self.config.component_fill_threshold,"official_address_verification_unchanged":True})
        return d
