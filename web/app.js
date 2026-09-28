const fileInput = document.getElementById('fileInput');
const dropzone = document.getElementById('dropzone');
const extractBtn = document.getElementById('extractBtn');
const downloadBtn = document.getElementById('downloadBtn');
const fileMeta = document.getElementById('fileMeta');
const progress = document.getElementById('progress');
const errorBox = document.getElementById('error');
const emptyState = document.getElementById('emptyState');
const results = document.getElementById('results');
let currentResult = null;

const pick = (obj, paths, fallback='—') => {
  for (const path of paths) {
    let cur = obj;
    let ok = true;
    for (const part of path.split('.')) {
      if (cur == null || !(part in cur)) { ok=false; break; }
      cur=cur[part];
    }
    if (ok && cur !== null && cur !== '' && cur !== undefined) return cur;
  }
  return fallback;
};
const fmt = v => typeof v === 'number' ? new Intl.NumberFormat('fr-FR', {maximumFractionDigits: 4}).format(v) : (v ?? '—');
const money = (v, cur='EUR') => v == null ? '—' : `${new Intl.NumberFormat('fr-FR', {minimumFractionDigits:2, maximumFractionDigits:2}).format(v)} ${cur}`;

async function health(){
  const el=document.getElementById('health');
  try {
    const r=await fetch('/api/health');
    const j=await r.json();
    el.className='health ok';
    el.textContent=`API OK · ${j.version}`;
  } catch {
    el.className='health bad';
    el.textContent='API indisponible';
  }
}
health(); setInterval(health, 20000);

function selectFile(file){
  if(!file)return;
  fileInput.files = (()=>{const dt=new DataTransfer(); dt.items.add(file); return dt.files;})();
  fileMeta.textContent=`${file.name} · ${(file.size/1024/1024).toFixed(2)} Mo`;
  extractBtn.disabled=false;
}
fileInput.addEventListener('change',()=>selectFile(fileInput.files[0]));
['dragenter','dragover'].forEach(e=>dropzone.addEventListener(e,ev=>{ev.preventDefault();dropzone.classList.add('drag');}));
['dragleave','drop'].forEach(e=>dropzone.addEventListener(e,ev=>{ev.preventDefault();dropzone.classList.remove('drag');}));
dropzone.addEventListener('drop',e=>selectFile(e.dataTransfer.files[0]));

extractBtn.addEventListener('click', async()=>{
  const file=fileInput.files[0]; if(!file)return;
  errorBox.classList.add('hidden'); progress.classList.remove('hidden'); extractBtn.disabled=true;
  try {
    const fd=new FormData(); fd.append('file',file);
    const dt=document.getElementById('documentType').value.trim(); if(dt) fd.append('document_type',dt);
    const schema=document.getElementById('schemaJson').value.trim();
    if(schema){ JSON.parse(schema); fd.append('schema_json',schema); }
    const resp=await fetch('/api/extract',{method:'POST',body:fd});
    const body=await resp.json();
    if(!resp.ok) throw new Error(body.detail || JSON.stringify(body));
    currentResult=body; render(body); downloadBtn.disabled=false;
  } catch(e) {
    errorBox.textContent=e.message; errorBox.classList.remove('hidden');
  } finally {
    progress.classList.add('hidden'); extractBtn.disabled=false;
  }
});

downloadBtn.addEventListener('click',()=>{
  if(!currentResult)return;
  const blob=new Blob([JSON.stringify(currentResult,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='jin-model-result.json';
  a.click();
  URL.revokeObjectURL(a.href);
});

function kv(container, entries){
  container.innerHTML='';
  for(const [k,v] of entries){
    const d=document.createElement('div');
    d.className='kv';
    d.innerHTML='<div class="k"></div><div class="v"></div>';
    d.querySelector('.k').textContent=k;
    d.querySelector('.v').textContent=fmt(v);
    container.appendChild(d);
  }
}
function metrics(entries){
  const c=document.getElementById('metrics');
  c.innerHTML='';
  for(const [k,v] of entries){
    const d=document.createElement('div');
    d.className='metric';
    d.innerHTML='<div class="label"></div><div class="value"></div>';
    d.querySelector('.label').textContent=k;
    d.querySelector('.value').textContent=fmt(v);
    c.appendChild(d);
  }
}
function getBusiness(r){
  const b=r.business_extractions||{};
  return b.purchase_order||b.purchase_order_with_terms||Object.values(b)[0]||{};
}
function render(r){
  emptyState.classList.add('hidden'); results.classList.remove('hidden');
  const b=getBusiness(r), po=b.purchase_order||b, totals=po.totals||b.totals||{};
  metrics([
    ['Type',pick(r,['detected_document_type','document_type'])],
    ['Confiance globale',pick(r,['overall_confidence','confidence.overall'])],
    ['Validation',pick(po,['validation.status','validation_status'],pick(r,['validation.status']))],
    ['Revue humaine',pick(r,['requires_human_review'],false)?'OUI':'NON'],
    ['Pages',pick(r,['page_count','document.page_count'])],
    ['Version',pick(r,['engine_version','version'])]
  ]);
  kv(document.getElementById('summary'),[
    ['N° commande',pick(po,['number','purchase_order_number','order_number'])],
    ['Date',pick(po,['order_date','date'])],
    ['Devise',pick(po,['currency'],pick(totals,['currency']))],
    ['Acheteur',pick(po,['buyer.name'],pick(b,['buyer.name']))],
    ['Fournisseur',pick(po,['supplier.name'],pick(b,['supplier.name']))],
    ['Contact',pick(po,['buyer.contact.name','contact_name'])]
  ]);
  const addrs=r.business_addresses||po.business_addresses||b.business_addresses||[];
  const ac=document.getElementById('addresses'); ac.innerHTML='';
  if(!addrs.length) ac.innerHTML='<div class="muted">Aucune adresse métier structurée.</div>';
  for(const a of addrs){
    const d=document.createElement('div'); d.className='card';
    const addr=a.formatted_address||[a.address?.line1,a.address?.line2,a.address?.postal_code,a.address?.city,a.address?.country].filter(Boolean).join('\n');
    d.innerHTML='<div class="role"></div><div class="name"></div><div class="addr"></div><div class="muted conf"></div>';
    d.querySelector('.role').textContent=a.role_label||a.role||'adresse';
    d.querySelector('.name').textContent=a.party_name||'—';
    d.querySelector('.addr').textContent=addr||'—';
    d.querySelector('.conf').textContent=`Confiance: ${fmt(a.confidence||a.role_confidence||a.address_confidence)}`;
    ac.appendChild(d);
  }
  const lines=po.lines||b.lines||[];
  const tbody=document.querySelector('#linesTable tbody'); tbody.innerHTML='';
  for(const l of lines){
    const tr=document.createElement('tr');
    const vals=[l.line_number,l.material_number||l.article_number||l.product_code,l.description,l.quantity,l.uom||l.unit,l.unit_price,l.line_total,l.confidence];
    vals.forEach((v,i)=>{const td=document.createElement('td'); td.textContent=(i===5||i===6)&&typeof v==='number'?money(v,l.currency||po.currency||'EUR'):fmt(v); tr.appendChild(td);});
    tbody.appendChild(tr);
  }
  const charges=po.charges||b.charges||[];
  const cbody=document.querySelector('#chargesTable tbody'); cbody.innerHTML='';
  for(const ch of charges){
    const tr=document.createElement('tr');
    [ch.charge_type,ch.description,ch.supplier_reference||ch.material_number,ch.quantity,ch.unit_price,ch.amount].forEach((v,i)=>{
      const td=document.createElement('td');
      td.textContent=i>=4&&typeof v==='number'?money(v,ch.currency||po.currency||'EUR'):fmt(v);
      tr.appendChild(td);
    });
    cbody.appendChild(tr);
  }
  kv(document.getElementById('totals'),[
    ['Sous-total',money(totals.subtotal??totals.total_net??totals.net,totals.currency||po.currency||'EUR')],
    ['Charges',money(totals.total_charges??totals.total_surcharge,totals.currency||po.currency||'EUR')],
    ['TVA',money(totals.total_vat??totals.vat??totals.total_tax,totals.currency||po.currency||'EUR')],
    ['Total HT',money(totals.total_before_tax??totals.total_net??totals.net,totals.currency||po.currency||'EUR')],
    ['Net à payer',money(totals.amount_due??totals.grand_total??totals.total_gross??totals.gross,totals.currency||po.currency||'EUR')]
  ]);
  document.getElementById('jsonView').textContent=JSON.stringify(r,null,2);
}
