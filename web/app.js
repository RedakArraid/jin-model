'use strict';

// ── PDF.js worker ──────────────────────────────────────────────────────────
pdfjsLib.GlobalWorkerOptions.workerSrc =
  'vendor/pdfjs-3.11.174/pdf.worker.min.js';

// ── State ──────────────────────────────────────────────────────────────────
const files = []; // { file, status, result, objectUrl, error, tabEl, pdfDoc }
let activeIdx   = -1;
let queueBusy   = false;
let renderGen   = 0;

// ── DOM ───────────────────────────────────────────────────────────────────
const tabsEl          = document.getElementById('tabs');
const fileInput       = document.getElementById('fileInput');
const fileInputMain   = document.getElementById('fileInputMain');
const emptyWs         = document.getElementById('emptyWs');
const viewerPane      = document.getElementById('viewerPane');
const resultsPane     = document.getElementById('resultsPane');
const fileViewer      = document.getElementById('fileViewer');
const downloadBtn     = document.getElementById('downloadBtn');
const downloadRawBtn  = document.getElementById('downloadRawBtn');
const resultsFileName = document.getElementById('resultsFileName');
const stateProcessing = document.getElementById('stateProcessing');
const stateError      = document.getElementById('stateError');
const statePending    = document.getElementById('statePending');
const stateEmpty      = document.getElementById('stateEmpty');
const resultsEl       = document.getElementById('results');
const dragOverlay     = document.getElementById('dragOverlay');

// ── Utilities ─────────────────────────────────────────────────────────────
const pick = (obj, paths, fallback = null) => {
  for (const path of paths) {
    let cur = obj; let ok = true;
    for (const part of path.split('.')) {
      if (cur == null || !(part in cur)) { ok = false; break; }
      cur = cur[part];
    }
    if (ok && cur !== null && cur !== '' && cur !== undefined) return cur;
  }
  return fallback;
};
const fmt = v => typeof v === 'number'
  ? new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 4 }).format(v)
  : String(v ?? '—');
const money = (v, cur = 'EUR') =>
  (v == null) ? null : `${new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(v)} ${cur}`;
const unitMoney = (v, cur = 'EUR') =>
  (v == null) ? null : `${new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 10 }).format(v)} ${cur}`;
const unitPriceLabel = (line, currency) => {
  const amount = unitMoney(line.net_unit_price ?? line.unit_price, line.currency || currency);
  if (amount == null) return null;
  const basis = line.price_unit;
  return basis != null && basis > 0 && basis !== 1 ? `${amount} / ${fmt(basis)} ${line.uom || 'unités'}` : amount;
};

const lineReferenceLabel = (line, includeCommercial = true) => {
  const sourcePrefix = line.material_reference_source_prefix;
  const primary = (sourcePrefix && line.supplier_material_number)
    || line.material_number || line.article_number || line.product_code
    || line.manufacturer_part_number || line.supplier_material_number;
  const details = [];
  if (!sourcePrefix && line.supplier_material_number && line.supplier_material_number !== primary) {
    details.push(`Fourn. ${line.supplier_material_number}`);
  }
  if (line.customer_material_number && line.customer_material_number !== primary) {
    details.push(`Client ${line.customer_material_number}`);
  }
  if (includeCommercial) {
    const quotes = [...new Set([...(line.quote_numbers || []), line.quote_number].filter(Boolean))];
    const derogations = [...new Set([...(line.derogation_numbers || []), line.derogation_number].filter(Boolean))];
    if (quotes.length) details.push(`Devis ${quotes.join(', ')}`);
    if (derogations.length) details.push(`Dérog. ${derogations.join(', ')}`);
  }
  return [primary, ...details].filter(Boolean).join(' · ') || null;
};

const extractedValue = value => (
  value && typeof value === 'object' && 'value' in value ? value.value : value
);

const uniqueValues = values => [...new Set(
  (values || []).map(extractedValue).filter(value => value != null && value !== '').map(String),
)];

const lineCommercialReferences = line => ({
  quotes: uniqueValues([...(line.quote_numbers || []), line.quote_number]),
  derogations: uniqueValues([...(line.derogation_numbers || []), line.derogation_number]),
});

const confidenceLabel = value => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(parsed * 100)} %` : null;
};

const chargeTypeLabel = value => ({
  shipping: 'Port / transport',
  environmental_fee: 'Contribution environnementale',
  tax: 'Taxe',
  surcharge: 'Frais complémentaire',
  discount: 'Remise',
}[value] || value || 'Frais');

const taxRoleLabel = value => ({
  buyer: 'Acheteur',
  supplier: 'Fournisseur',
  buyer_or_issuer: 'Acheteur / émetteur',
  unknown: 'Rôle non déterminé',
}[value] || value || 'Rôle non déterminé');

const taxStatusLabel = value => ({
  CHECKSUM_AND_REGISTRATION_MATCH: 'TVA + SIREN/SIRET concordants',
  CHECKSUM_VALID: 'TVA valide',
  DERIVED_FROM_VALID_SIRET: 'Calculée depuis un SIRET valide',
  DERIVED_FROM_VALID_SIREN: 'Calculée depuis un SIREN valide',
  SOURCE_SUPPORTED: 'Présente dans la source',
}[value] || value || 'Non vérifié');

function addTextCell(row, value, className = '') {
  const cell = document.createElement('td');
  if (className) cell.className = className;
  cell.textContent = value ?? '—';
  row.appendChild(cell);
  return cell;
}

function addTagCell(row, groups) {
  const cell = document.createElement('td');
  const list = document.createElement('div');
  list.className = 'tag-list';
  for (const group of groups) {
    for (const value of group.values || []) {
      const tag = document.createElement('span');
      tag.className = `tag ${group.className || ''}`.trim();
      tag.textContent = `${group.label} ${value}`;
      list.appendChild(tag);
    }
  }
  if (list.children.length) cell.appendChild(list);
  else cell.textContent = '—';
  row.appendChild(cell);
  return cell;
}

function normalizedLine(line) {
  if (!line.references && !line.pricing && !line.amounts) return line;
  return {
    ...line,
    ...(line.references || {}),
    quantity: line.quantity,
    unit_price: line.pricing?.unit_price,
    net_unit_price: line.pricing?.net_unit_price,
    price_unit: line.pricing?.price_unit,
    currency: line.pricing?.currency,
    line_total: line.amounts?.total ?? line.amounts?.net,
  };
}

function rawFieldDisplay(field) {
  if (!field || typeof field !== 'object') return null;
  const value = field.normalized_value != null && field.normalized_value !== ''
    ? field.normalized_value : field.value;
  if (value == null || value === '') return null;
  if (Array.isArray(value)) {
    return value.map(item => typeof item === 'object' ? JSON.stringify(item) : String(item)).join('\n');
  }
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function uiDeliveryAddresses(result) {
  const be = result?.business_extractions || {};
  const order = be.purchase_order || {};
  const addresses = be.business_addresses?.length
    ? be.business_addresses : (order.business_addresses || []);
  const selectedAddress = addresses.find(address => (
    ['ship_to', 'deliver_to', 'consignee'].includes(String(address?.role || '').toLowerCase())
    && !/superseded/i.test(String(address?.role_label || ''))
  ));
  const normalized = result?.normalized_output?.order?.delivery_address
    || (result?.schema_version === 'jin-clean-extraction-v2'
      ? result?.order?.delivery_address : null);
  const candidates = [
    normalized,
    normalized?.address,
    order.delivery_address,
    order.delivery_address?.address,
    order.shipping_address,
    order.shipping_address?.address,
    order.ship_to?.address,
    selectedAddress,
    selectedAddress?.address,
    order.purchase_order?.delivery_address,
    order.purchase_order?.delivery_address?.address,
    order.purchase_order?.shipping_address,
    order.purchase_order?.shipping_address?.address,
  ];
  const source = candidates.find(candidate => rawFieldDisplay(candidate));
  const directDisplay = rawFieldDisplay(source);
  if (directDisplay) {
    return [{
      role: source.role || 'ship_to',
      role_label: source.role_label || 'Adresse de livraison sélectionnée',
      party_name: '',
      customer_agency_code: source.customer_agency_code || order.ship_to?.customer_agency_code || '',
      lines: [directDisplay],
      role_confidence: source.role_confidence,
      address_confidence: source.address_confidence ?? source.confidence,
      clean_status: source.normalization?.status || source.clean_address?.status,
      verification: source.verification || source.ban_verification || {},
      evidence: source.evidence || source.source || {},
      selected: true,
      direct_value: true,
    }];
  }
  return [];
}

// ── Health ─────────────────────────────────────────────────────────────────
async function health() {
  const el = document.getElementById('health');
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    el.className = 'health ok';
    const runtime = j.runtime_layer?.version || j.version;
    const locality = j.runtime_layer?.offline ? 'local' : 'connecté';
    el.textContent = `API OK · ${runtime} · ${locality}`;
  } catch {
    el.className = 'health bad';
    el.textContent = 'API indisponible';
  }
}
health(); setInterval(health, 20000);

// ── File inputs ────────────────────────────────────────────────────────────
fileInput.addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });
fileInputMain.addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });

// ── Drag & drop ────────────────────────────────────────────────────────────
let dragDepth = 0;
document.addEventListener('dragenter', e => { e.preventDefault(); dragDepth++; dragOverlay.classList.remove('hidden'); });
document.addEventListener('dragleave', () => { dragDepth--; if (dragDepth <= 0) { dragDepth = 0; dragOverlay.classList.add('hidden'); } });
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; dragOverlay.classList.add('hidden');
  addFiles(e.dataTransfer.files);
});

// ── File management ─────────────────────────────────────────────────────────
function addFiles(fileList) {
  for (const f of fileList) {
    files.push({ file: f, status: 'pending', result: null, objectUrl: URL.createObjectURL(f), error: null, tabEl: null });
    buildTab(files.length - 1);
  }
  if (files.length > 0) {
    emptyWs.classList.add('hidden');
    viewerPane.classList.remove('hidden');
    resultsPane.classList.remove('hidden');
    if (activeIdx === -1) selectTab(0);
  }
  runQueue();
}

function buildTab(idx) {
  const entry = files[idx];
  const short = entry.file.name.length > 22 ? entry.file.name.slice(0, 20) + '…' : entry.file.name;
  const icon  = { pending: '○', processing: '⏳', done: '✓', error: '✗' }[entry.status];

  const btn = document.createElement('button');
  btn.className = 'tab' + (idx === activeIdx ? ' active' : '');
  btn.dataset.idx = idx;
  btn.innerHTML =
    `<span class="tab-name" title="${entry.file.name}">${short}</span>` +
    `<span class="tab-st ${entry.status}">${icon}</span>` +
    `<span class="tab-close" data-close="${idx}">×</span>`;

  btn.addEventListener('click', e => {
    if (e.target.dataset.close !== undefined) { e.stopPropagation(); closeTab(+e.target.dataset.close); return; }
    selectTab(+btn.dataset.idx);
  });

  if (entry.tabEl) entry.tabEl.replaceWith(btn);
  else tabsEl.appendChild(btn);
  entry.tabEl = btn;
}

function selectTab(idx) {
  activeIdx = idx;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', +t.dataset.idx === idx));
  showActive();
}

function closeTab(idx) {
  if (files[idx].pdfDoc) { files[idx].pdfDoc.destroy(); files[idx].pdfDoc = null; }
  URL.revokeObjectURL(files[idx].objectUrl);
  files[idx].tabEl.remove();
  files.splice(idx, 1);
  files.forEach((f, i) => { f.tabEl.dataset.idx = i; });
  if (files.length === 0) {
    activeIdx = -1;
    emptyWs.classList.remove('hidden');
    viewerPane.classList.add('hidden');
    resultsPane.classList.add('hidden');
    fileViewer.innerHTML = '';
    resultsFileName.textContent = '';
    downloadBtn.disabled = true;
    downloadRawBtn.disabled = true;
    [stateProcessing, stateError, statePending, resultsEl].forEach(el => el.classList.add('hidden'));
    stateEmpty.classList.remove('hidden');
  } else {
    selectTab(Math.min(idx, files.length - 1));
  }
}

// ── Processing queue ────────────────────────────────────────────────────────
async function runQueue() {
  if (queueBusy) return;
  const i = files.findIndex(f => f.status === 'pending');
  if (i === -1) return;
  queueBusy = true;

  files[i].status = 'processing';
  buildTab(i);
  if (activeIdx === i) showActive();

  try {
    const fd = new FormData();
    fd.append('file', files[i].file);
    const resp = await fetch('/api/extract', { method: 'POST', body: fd });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || JSON.stringify(body));
    files[i].result = body;
    files[i].status = 'done';
  } catch (e) {
    files[i].status = 'error';
    files[i].error  = e.message;
  }

  queueBusy = false;
  buildTab(i);
  if (activeIdx === i) showActive();
  runQueue();
}

// ── Show active file ────────────────────────────────────────────────────────
function showActive() {
  if (activeIdx < 0 || !files[activeIdx]) return;
  const entry = files[activeIdx];

  resultsFileName.textContent = entry.file.name;
  showViewer(entry);

  [stateProcessing, stateError, statePending, stateEmpty, resultsEl].forEach(el => el.classList.add('hidden'));
  downloadBtn.disabled = true;
  downloadRawBtn.disabled = true;

  if      (entry.status === 'processing') { stateProcessing.classList.remove('hidden'); }
  else if (entry.status === 'error')      { stateError.textContent = entry.error; stateError.classList.remove('hidden'); }
  else if (entry.status === 'pending')    { statePending.classList.remove('hidden'); }
  else if (entry.status === 'done')       {
    render(entry.result);
    resultsEl.classList.remove('hidden');
    downloadBtn.disabled = false;
    downloadRawBtn.disabled = false;
  }
}

function showViewer(entry) {
  const ext = entry.file.name.split('.').pop().toLowerCase();
  const legendEl = document.getElementById('bboxLegend');
  legendEl.classList.add('hidden');

  if (ext === 'pdf') {
    renderGen++;
    const gen = renderGen;
    fileViewer.innerHTML = '<div class="pdf-pages" id="pdfPages"></div>';
    renderPdf(entry, gen);
  } else if (['png','jpg','jpeg','tiff','tif','bmp','webp'].includes(ext)) {
    fileViewer.innerHTML = `<img class="img-preview" src="${entry.objectUrl}" alt="${entry.file.name}" />`;
  } else {
    const sizeMb = (entry.file.size / 1024 / 1024).toFixed(2);
    fileViewer.innerHTML = `<div class="file-placeholder"><div class="f-icon">📄</div><div class="f-name">${entry.file.name}</div><div class="f-size muted">${sizeMb} Mo</div></div>`;
  }
}

async function renderPdf(entry, gen) {
  if (!entry.pdfDoc) {
    try {
      entry.pdfDoc = await pdfjsLib.getDocument({ url: entry.objectUrl, isEvalSupported: false }).promise;
    } catch { return; }
  }
  if (renderGen !== gen) return;

  const container = document.getElementById('pdfPages');
  if (!container) return;

  const boxes    = entry.result ? collectBoxes(entry.result) : {};
  const hasBoxes = Object.values(boxes).some(arr => arr.length > 0);
  const sourcePages = [
    ...(entry.result?.business_extractions?.purchase_order?.pages || []),
    ...(entry.result?.pages || []),
  ];
  const hasUnmappedOcr = sourcePages.some(corePageNeedsReprojection);
  const legend = document.getElementById('bboxLegend');
  let ocrNote = document.getElementById('bboxOcrNote');
  if (!ocrNote) {
    ocrNote = document.createElement('span');
    ocrNote.id = 'bboxOcrNote';
    ocrNote.className = 'legend-item';
    ocrNote.textContent = 'Repères OCR masqués faute de recalage sur le PDF ; coordonnées conservées dans le JSON.';
    legend.appendChild(ocrNote);
  }
  ocrNote.classList.toggle('hidden', !hasUnmappedOcr);
  legend.classList.toggle('hidden', !hasBoxes && !hasUnmappedOcr);

  // Optimisation : pages déjà rendues → mise à jour des SVG uniquement
  const existingPages = container.querySelectorAll('.pdf-page');
  if (existingPages.length === entry.pdfDoc.numPages) {
    existingPages.forEach((wrapper, i) => {
      const svg = wrapper.querySelector('.bbox-overlay');
      if (!svg) return;
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const W = parseFloat(wrapper.style.width);
      const H = parseFloat(wrapper.style.height);
      for (const box of (boxes[i + 1] || [])) drawBox(svg, box, W, H);
    });
    return;
  }

  // Rendu complet page par page
  container.innerHTML = '';
  const containerWidth = Math.max(200, fileViewer.clientWidth - 32);

  for (let p = 1; p <= entry.pdfDoc.numPages; p++) {
    if (renderGen !== gen) return;

    const page = await entry.pdfDoc.getPage(p);
    const vp0  = page.getViewport({ scale: 1 });
    const vp   = page.getViewport({ scale: containerWidth / vp0.width });

    const wrapper = document.createElement('div');
    wrapper.className    = 'pdf-page';
    wrapper.style.width  = vp.width  + 'px';
    wrapper.style.height = vp.height + 'px';

    const canvas  = document.createElement('canvas');
    canvas.width  = vp.width;
    canvas.height = vp.height;
    await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class',   'bbox-overlay');
    svg.setAttribute('width',   vp.width);
    svg.setAttribute('height',  vp.height);
    svg.setAttribute('viewBox', `0 0 ${vp.width} ${vp.height}`);
    for (const box of (boxes[p] || [])) drawBox(svg, box, vp.width, vp.height);

    wrapper.appendChild(canvas);
    wrapper.appendChild(svg);

    if (renderGen !== gen) return;
    const c = document.getElementById('pdfPages');
    if (c) c.appendChild(wrapper);
  }
}

// ── Download ────────────────────────────────────────────────────────────────
downloadBtn.addEventListener('click', () => {
  const entry = files[activeIdx];
  if (!entry?.result) return;
  const downloadable = entry.result.normalized_output || entry.result;
  const blob = new Blob([JSON.stringify(downloadable, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `jin-${entry.file.name.replace(/\.[^.]+$/, '')}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

downloadRawBtn.addEventListener('click', () => {
  const entry = files[activeIdx];
  if (!entry?.result) return;
  const blob = new Blob([JSON.stringify(entry.result, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `jin-complet-${entry.file.name.replace(/\.[^.]+$/, '')}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// ── KV + Metrics helpers ────────────────────────────────────────────────────
function fillKv(container, entries) {
  container.innerHTML = '';
  for (const [k, v] of entries) {
    if (v == null || v === '' || v === '—') continue;
    const d = document.createElement('div');
    d.className = 'kv';
    d.innerHTML = '<div class="k"></div><div class="v"></div>';
    d.querySelector('.k').textContent = k;
    d.querySelector('.v').textContent = typeof v === 'number' ? fmt(v) : v;
    container.appendChild(d);
  }
}

function fillMetrics(entries) {
  const c = document.getElementById('metrics');
  c.innerHTML = '';
  for (const [k, v] of entries) {
    const d = document.createElement('div');
    d.className = 'metric';
    d.innerHTML = '<div class="label"></div><div class="value"></div>';
    d.querySelector('.label').textContent = k;
    d.querySelector('.value').textContent = (v == null) ? '—' : fmt(v);
    c.appendChild(d);
  }
}

function toggleSection(id, visible) {
  document.getElementById(id).classList.toggle('hidden', !visible);
}

// ── BBox collection ────────────────────────────────────────────────────────
function collectBoxes(r) {
  const byPage = {};
  const be = r.business_extractions || {};
  const b  = be.purchase_order      || {};
  const po = b.purchase_order       || {};
  const corePages = new Map((b.pages || []).map(page => [page.page, page]));
  const blockPages = new Map((r.pages || []).map(page => [page.page, page]));
  const add = (bbox, page, label, type) => {
    if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isFinite)) return;
    if (!Number.isInteger(page) || page < 1) return;
    let width = 1000, height = 1000;
    if (type !== 'weak') {
      // Only source-aligned core coordinates can be rescaled. OCR preprocessing
      // may crop or rotate: its pixel boxes remain in JSON until an inverse
      // transform is available. WeakFieldRouter already uses the original PDF.
      const sourcePage = (type === 'block' ? blockPages : corePages).get(page);
      if (!sourcePage || corePageNeedsReprojection(sourcePage)) return;
      width = sourcePage.width;
      height = sourcePage.height;
      if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
    }
    if (bbox[0] < 0 || bbox[1] < 0 || bbox[2] > width || bbox[3] > height
        || bbox[2] <= bbox[0] || bbox[3] <= bbox[1]) return;
    const normalized = [
      bbox[0] / width * 1000, bbox[1] / height * 1000,
      bbox[2] / width * 1000, bbox[3] / height * 1000,
    ];
    (byPage[page] = byPage[page] || []).push({ bbox: normalized, label, type });
  };

  // Champs d'en-tête
  for (const [key, label] of [
    ['number',             'N° commande'],
    ['order_date',         'Date'],
    ['currency',           'Devise'],
    ['vendor_reference',   'Réf. fourn.'],
    ['customer_reference', 'Réf. client'],
    ['customer_agency_code', 'Code agence client'],
    ['contract_number',    'N° contrat'],
    ['quote_number',       'N° devis'],
    ['derogation_number',  'N° dérogation'],
    ['project_number',     'N° projet'],
  ]) {
    const f = po[key];
    if (f?.evidence?.bbox) add(f.evidence.bbox, f.evidence.page, label, 'field');
  }

  // Acheteur / fournisseur
  for (const [key, label] of [['buyer', 'Acheteur'], ['supplier', 'Fournisseur']]) {
    const p = b[key];
    if (p?.evidence?.bbox) add(p.evidence.bbox, p.evidence.page, label, 'field');
  }

  // Lignes produit
  for (const l of (b.lines || [])) {
    if (l.bbox) add(l.bbox, l.page, `L${l.line_number || ''}`, 'line');
  }

  // Frais, taxes et contributions séparés des lignes produit
  for (const charge of (b.additional_charges || [])) {
    const evidence = charge.evidence || {};
    if (evidence.bbox) add(evidence.bbox, evidence.page || charge.page, chargeTypeLabel(charge.charge_type), 'charge');
  }

  // Adresses
  const addresses = be.business_addresses?.length ? be.business_addresses : (b.business_addresses || []);
  for (const a of addresses) {
    const role = String(a.role || '').toLowerCase();
    if (!['ship_to', 'deliver_to', 'consignee'].includes(role)
        || /superseded/i.test(String(a.role_label || ''))
        || (a.warnings || []).some(warning => /superseded/i.test(String(warning)))) continue;
    if (a.evidence?.bbox) add(a.evidence.bbox, a.evidence.page, a.role_label || a.role || 'Adresse', 'address');
  }

  // Totaux
  const tot = b.totals || {};
  for (const [key, label] of [
    ['subtotal',        'Sous-total'],
    ['total_before_tax','HT'],
    ['amount_due',      'Net à payer'],
    ['grand_total',     'Total'],
    ['total_vat',       'TVA'],
  ]) {
    const f = tot[key];
    if (f?.evidence?.bbox) add(f.evidence.bbox, f.evidence.page, label, 'total');
  }

  // Suggestions faibles (WeakFieldRouter)
  for (const span of ((r.weak_field_suggestions || {}).spans || [])) {
    if (span.bbox) add(span.bbox, span.page, span.label || '', 'weak');
  }

  // Blocs génériques
  for (const blk of (r.blocks || [])) {
    if (blk.source?.bbox) add(blk.source.bbox, blk.source.page, blk.type || 'bloc', 'block');
  }

  return byPage;
}

function corePageNeedsReprojection(page) {
  return ['scan_ocr', 'image_ocr'].includes(page.source_type)
    || (page.rotation_applied || 0) !== 0;
}

function drawBox(svg, { bbox, label, type }, W, H) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isFinite)
      || !Number.isFinite(W) || !Number.isFinite(H) || W <= 0 || H <= 0) return;
  const COLORS = {
    field:   '#3b82f6',
    line:    '#22c55e',
    charge:  '#ef4444',
    address: '#f59e0b',
    total:   '#a855f7',
    weak:    '#64748b',
    block:   '#475569',
  };
  const c = COLORS[type] || '#3b82f6';
  const [x1, y1, x2, y2] = bbox;
  const x = x1 / 1000 * W, y = y1 / 1000 * H;
  const w = (x2 - x1) / 1000 * W, h = (y2 - y1) / 1000 * H;
  if (w <= 0 || h <= 0) return;

  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('x', x);           rect.setAttribute('y', y);
  rect.setAttribute('width', w);        rect.setAttribute('height', h);
  rect.setAttribute('fill', c + '22'); // 13% opacity fill
  rect.setAttribute('stroke', c);
  rect.setAttribute('stroke-width', '1.5');
  rect.setAttribute('rx', '2');
  svg.appendChild(rect);

  if (label) {
    const labelW = Math.min(label.length * 5.5 + 6, 120);
    const labelY = y > 14 ? y - 13 : y + h + 2;

    const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    bg.setAttribute('x', x);      bg.setAttribute('y', labelY);
    bg.setAttribute('width', labelW); bg.setAttribute('height', '11');
    bg.setAttribute('fill', c);   bg.setAttribute('rx', '2');
    svg.appendChild(bg);

    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', x + 3); txt.setAttribute('y', labelY + 8);
    txt.setAttribute('fill', 'white');
    txt.setAttribute('font-size', '7.5');
    txt.setAttribute('font-family', 'Inter, system-ui, sans-serif');
    txt.setAttribute('font-weight', '700');
    txt.textContent = label;
    svg.appendChild(txt);
  }
}

// ── Render ─────────────────────────────────────────────────────────────────
function render(r) {
  const be  = r.business_extractions || {};
  const b   = be.purchase_order || {};   // PurchaseOrderResult
  const po  = b.purchase_order  || {};   // PurchaseOrderHeader (ExtractedField objects)
  const clean = r.normalized_output || {};
  const cleanOrder = clean.order || {};
  const cleanDocument = clean.document || {};
  const tot = Object.keys(b.totals || {}).length ? b.totals : (cleanOrder.totals || {});
  const com = Object.keys(b.commercial || {}).length ? b.commercial : (cleanOrder.commercial_terms || {});
  const log = Object.keys(b.logistics || {}).length ? b.logistics : (cleanOrder.logistics || {});
  const cur = pick(po, ['currency.value']) || tot.currency || 'EUR';

  // ── Metrics bar ──
  fillMetrics([
    ['Type',          pick(r, ['document.detected_document_type', 'detected_document_type'], cleanDocument.type)],
    ['Décision',      pick(r, ['extraction_decision.status', 'quality.decision'], '—')],
    ['Score technique', pick(r, ['quality.overall_confidence',    'overall_confidence'])],
    ['Validation',    pick(b, ['validation.status',              'validation_status'])],
    ['Pages',         pick(r, ['document.page_count',            'page_count'])],
    ['Runtime',       r.runtime_layer_version || clean.generator?.runtime || '—'],
  ]);

  let checksEl = document.getElementById('extractionChecks');
  if (!checksEl) {
    checksEl = document.createElement('section');
    checksEl.id = 'extractionChecks';
    checksEl.className = 'result-section';
    document.getElementById('secSummary').before(checksEl);
  }
  checksEl.replaceChildren();
  const reasons = r.extraction_decision?.reasons || [];
  checksEl.classList.toggle('hidden', reasons.length === 0);
  if (reasons.length) {
    const heading = document.createElement('h3');
    heading.textContent = 'Extraction à vérifier';
    checksEl.appendChild(heading);
    const labels = {
      CORE_REVIEW_REQUIRED: 'Le moteur signale une incertitude.',
      BUSINESS_REVIEW_REQUIRED: 'Les informations de commande restent à vérifier.',
      BUSINESS_VALIDATION_INCOMPLETE: 'Les contrôles de commande ne sont pas tous satisfaits.',
      ORDER_EXTRACTION_MISSING: 'La commande n’a pas pu être structurée.',
      ORDER_NUMBER_MISSING: 'Numéro de commande manquant.',
      ORDER_NUMBER_EVIDENCE_MISSING: 'Numéro de commande client : preuve dans le document incomplète.',
      ORDER_NUMBER_CANDIDATES_DISAGREE: 'Numéros de commande client contradictoires : vérification nécessaire.',
      ORDER_NUMBER_CORE_WARNING: 'Le numéro de commande client présente une incertitude d’extraction.',
      ORDER_NUMBER_FORMAT_SUSPICIOUS: 'Le numéro de commande client ressemble à une date ou à une valeur anormale.',
      ORDER_DATE_MISSING: 'Date de commande manquante.',
      ORDER_LINES_MISSING: 'Aucune ligne d’article extraite.',
      PRODUCT_REFERENCE_MISSING: 'Référence produit manquante.',
      QUANTITY_MISSING: 'Quantité manquante.',
      UNIT_PRICE_MISSING: 'Prix unitaire manquant.',
      PARTY_NAME_MISSING: 'Nom du client ou du fournisseur manquant.',
      PARTY_NAME_UNINFORMATIVE: 'La raison sociale semble incomplète.',
      PARTY_CONTACT_ROLE_CONFLICT: 'Le même contact est attribué au client et au fournisseur : attribution à vérifier.',
      ADDRESS_ROLE_UNRESOLVED: 'Type d’adresse non identifié.',
      ADDRESS_ROLE_MISSING: 'Adresse de livraison ou de facturation non identifiée.',
      DELIVERY_ADDRESS_AMBIGUOUS: 'Plusieurs adresses de livraison candidates : sélection manuelle nécessaire.',
      DELIVERY_ADDRESS_INCOMPLETE: 'Adresse de livraison incomplète après normalisation.',
      ORDER_TOTAL_MISSING: 'Total HT manquant.',
      LINE_TOTAL_MISSING: 'Montant de ligne manquant.',
      ADDRESS_ROLE_UNCERTAIN: 'Type d’adresse incertain.',
      ADDRESS_LOCALITY_INCOMPLETE: 'Code postal ou ville manquants.',
      ADDRESS_REFERENCE_UNAVAILABLE: 'Référentiel officiel local indisponible pour cette adresse.',
      ADDRESS_NOT_FOUND_IN_REFERENCE: 'Adresse non retrouvée dans le référentiel local ; cela ne prouve pas qu’elle est invalide.',
      ADDRESS_REFERENCE_SUGGESTION: 'Le référentiel local propose une voie ou une commune proche : vérification nécessaire.',
      ADDRESS_NUMBER_NOT_FOUND_IN_REFERENCE: 'Voie reconnue, mais numéro non retrouvé dans le référentiel local.',
      ADDRESS_CITY_MISSING: 'Ville manquante.',
      FR_POSTAL_CODE_INVALID: 'Code postal français invalide.',
      LINE_AMOUNT_MISMATCH: 'La quantité et le prix ne concordent pas avec le montant.',
      TOTAL_ARITHMETIC_MISMATCH: 'Les totaux HT, TVA et TTC ne concordent pas.',
      FIELD_CANDIDATES_DISAGREE: 'Des valeurs différentes ont été trouvées sur plusieurs pages.',
      ENRICHMENT_UNAVAILABLE: 'Une étape d’extraction n’a pas abouti.',
      NOT_A_STRUCTURED_PURCHASE_ORDER: 'Ce document n’est pas une commande structurée validable.',
    };
    const list = document.createElement('ul');
    for (const reason of reasons) {
      const item = document.createElement('li');
      const line = /^lines\[(\d+)\]/.exec(reason.path || '');
      const address = /^business_addresses\[(\d+)\]/.exec(reason.path || '');
      const context = line ? ` (ligne ${Number(line[1]) + 1})`
        : address ? ` (adresse ${Number(address[1]) + 1})` : '';
      item.textContent = (labels[reason.code] || 'Une donnée nécessite une vérification.') + context;
      list.appendChild(item);
    }
    checksEl.appendChild(list);
  }

  // ── Commande summary ──
  const summaryEl = document.getElementById('summary');
  const quoteNumbers = (po.quote_numbers || []).map(value => value?.value ?? value).filter(Boolean);
  const derogationNumbers = (po.derogation_numbers || []).map(value => value?.value ?? value).filter(Boolean);
  fillKv(summaryEl, [
    ['N° commande client', pick(po, ['number.value',             'number'])],
    ['Page du n° client',  r.order_number_check?.source_evidence?.page],
    ['Source du n° client', r.order_number_check?.source_evidence?.source_text],
    ['Date commande',     pick(po, ['order_date.value',          'order_date'])],
    ['Devise',            pick(po, ['currency.value'],           tot.currency)],
    ['Acheteur',          pick(b,  ['buyer.name'])],
    ['Fournisseur',       pick(b,  ['supplier.name'])],
    ['Contact acheteur',  pick(b,  ['buyer.contact.name',        'contact_name'])],
    ['E-mail acheteur',   pick(b,  ['buyer.contact.email',       'buyer.email'])],
    ['Téléphone acheteur', pick(b, ['buyer.contact.phone',      'buyer.phone'])],
    ['Contact fournisseur', pick(b, ['supplier.contact.name'])],
    ['E-mail fournisseur', pick(b, ['supplier.contact.email',   'supplier.email'])],
    ['Réf. fournisseur',  pick(po, ['vendor_reference.value',   'vendor_reference'])],
    ['Réf. client',       pick(po, ['customer_reference.value', 'customer_reference'])],
    ['Code agence client', pick(po, ['customer_agency_code.value', 'customer_agency_code'])],
    ['N° contrat',        pick(po, ['contract_number.value',    'contract_number'])],
    ['N° devis',          quoteNumbers.length ? quoteNumbers.join(', ') : pick(po, ['quote_number.value', 'quote_number'])],
    ['N° dérogation',     derogationNumbers.length ? derogationNumbers.join(', ') : pick(po, ['derogation_number.value', 'derogation_number'])],
    ['N° projet',         pick(po, ['project_number.value',     'project_number'])],
  ]);
  toggleSection('secSummary', summaryEl.children.length > 0);

  // ── Identifiants fiscaux ──
  const taxSources = [
    ...(r.document_tax_identifiers || []),
    ...(b.tax_identifiers || []),
    ...(cleanDocument.tax_identifiers || []),
    ...(cleanOrder.tax_identifiers || []),
  ];
  const taxIdentifiers = [];
  const taxIndexes = new Map();
  for (const identifier of taxSources) {
    if (!identifier || !identifier.vat_number) continue;
    const key = String(identifier.vat_number);
    if (!taxIndexes.has(key)) {
      taxIndexes.set(key, taxIdentifiers.length);
      taxIdentifiers.push({ ...identifier });
      continue;
    }
    const index = taxIndexes.get(key);
    const current = taxIdentifiers[index];
    const currentRole = current.role || 'unknown';
    const nextRole = identifier.role || 'unknown';
    taxIdentifiers[index] = {
      ...identifier,
      ...current,
      role: currentRole === 'unknown' && nextRole !== 'unknown' ? nextRole : currentRole,
      siren: current.siren || identifier.siren,
      siret: current.siret || identifier.siret,
      registration_number: current.registration_number || identifier.registration_number,
      validation_status: current.validation_status === 'CHECKSUM_VALID'
        ? (identifier.validation_status || current.validation_status)
        : (current.validation_status || identifier.validation_status),
      supporting_evidence: current.supporting_evidence || identifier.supporting_evidence,
    };
  }
  const taxContainer = document.getElementById('taxIdentifiers');
  taxContainer.replaceChildren();
  for (const identifier of taxIdentifiers) {
    const card = document.createElement('article');
    card.className = 'info-card';
    const title = document.createElement('div');
    title.className = 'card-title';
    const role = document.createElement('strong');
    role.textContent = taxRoleLabel(identifier.role);
    const badge = document.createElement('span');
    const status = identifier.validation_status || identifier.status;
    badge.className = `status-badge ${String(status || '').startsWith('DERIVED') ? 'derived' : 'verified'}`;
    badge.textContent = taxStatusLabel(status);
    title.append(role, badge);
    const value = document.createElement('div');
    value.className = 'card-value';
    value.textContent = identifier.vat_number;
    const meta = document.createElement('div');
    meta.className = 'card-meta';
    const evidence = identifier.evidence || {};
    const details = [
      identifier.siret ? `SIRET : ${identifier.siret}` : null,
      identifier.siren ? `SIREN : ${identifier.siren}` : null,
      identifier.registration_number && !identifier.siret && !identifier.siren
        ? `Immatriculation : ${identifier.registration_number}` : null,
      identifier.confidence != null ? `Confiance : ${confidenceLabel(identifier.confidence)}` : null,
      evidence.page ? `Page ${evidence.page}` : null,
      String(status || '').startsWith('DERIVED') ? 'TVA calculée, non imprimée dans le document' : null,
      evidence.source_text ? `Source : ${evidence.source_text}` : null,
    ].filter(Boolean);
    meta.textContent = details.join(' · ');
    card.append(title, value, meta);
    taxContainer.appendChild(card);
  }
  toggleSection('secTaxIdentifiers', taxIdentifiers.length > 0);

  // ── Adresses ──
  const addrs = uiDeliveryAddresses(r);
  const ac = document.getElementById('addresses');
  ac.innerHTML = '';
  for (const a of addrs) {
    const d = document.createElement('div'); d.className = 'card';
    const addrText = a.lines.join('\n');
    d.innerHTML = '<div class="role"></div><div class="name"></div><div class="addr"></div><div class="contact"></div><div class="muted"></div>';
    d.querySelector('.role').textContent = a.role_label || a.role || 'adresse';
    d.querySelector('.name').textContent = a.party_name || '';
    d.querySelector('.addr').textContent = addrText;
    d.querySelector('.contact').textContent = [a.contact_name, a.contact_email, a.contact_phone].filter(Boolean).join(' · ');
    const verification = a.verification || {};
    const referenceLabels = {
      EXACT_MATCH: 'BAN : adresse exacte',
      CANONICAL_MATCH: 'BAN : adresse reconnue (écriture normalisée)',
      STREET_MATCH_NUMBER_NOT_FOUND: 'BAN : voie reconnue, numéro absent',
      CLOSE_STREET_MATCH: 'BAN : suggestion à vérifier',
      NOT_FOUND: 'BAN : adresse non retrouvée (non concluant)',
      INSUFFICIENT_COMPONENTS: 'BAN : composants insuffisants',
      DEPARTMENT_NOT_INDEXED: 'BAN : département non indexé',
      NOT_APPLICABLE: 'BAN : non applicable',
    };
    const details = [
      a.role_confidence != null ? `Rôle ${fmt(a.role_confidence)}` : null,
      a.address_confidence != null ? `Adresse ${fmt(a.address_confidence)}` : null,
      a.selected ? 'Adresse sélectionnée' : null,
      a.customer_agency_code ? `Code agence client : ${a.customer_agency_code}` : null,
      a.clean_status === 'VERIFIED_CANONICAL' ? 'Libellé livraison normalisé' : null,
      referenceLabels[verification.status],
    ].filter(Boolean);
    d.querySelector('.muted').textContent = details.join(' · ');
    ac.appendChild(d);
  }
  toggleSection('secAddresses', addrs.length > 0);

  // ── Devis et dérogations ──
  const referenceSources = [
    ...(b.quote_references || []),
    ...(b.derogation_references || []),
    ...(cleanOrder.quote_references || []),
    ...(cleanOrder.derogation_references || []),
    ...(r.commercial_references || []),
  ];
  const referenceMap = new Map();
  for (const reference of referenceSources) {
    if (!reference?.number) continue;
    const type = reference.reference_type || 'quote';
    const key = `${type}|${reference.number}`;
    const current = referenceMap.get(key) || {
      reference_type: type,
      number: reference.number,
      scope: reference.scope || 'document',
      line_numbers: [],
      material_numbers: [],
      confidence: reference.confidence,
      evidence: [],
    };
    current.line_numbers = uniqueValues([...(current.line_numbers || []), ...(reference.line_numbers || [])]);
    current.material_numbers = uniqueValues([...(current.material_numbers || []), ...(reference.material_numbers || [])]);
    current.evidence = [...(current.evidence || []), ...(reference.evidence || [])];
    if (current.line_numbers.length) current.scope = 'line';
    current.confidence = Math.max(Number(current.confidence) || 0, Number(reference.confidence) || 0) || null;
    referenceMap.set(key, current);
  }
  const references = [...referenceMap.values()];
  const referenceBody = document.querySelector('#referencesTable tbody');
  referenceBody.replaceChildren();
  for (const reference of references) {
    const row = document.createElement('tr');
    addTextCell(row, reference.reference_type === 'derogation' ? 'Dérogation' : 'Devis');
    addTextCell(row, reference.number, 'cell-primary');
    addTextCell(row, reference.scope === 'line' ? 'Ligne(s)' : 'Document');
    addTextCell(row, reference.line_numbers.length ? reference.line_numbers.join(', ') : '—');
    addTextCell(row, reference.material_numbers.length ? reference.material_numbers.join(', ') : '—');
    addTextCell(row, confidenceLabel(reference.confidence));
    const source = reference.evidence?.[0] || {};
    addTextCell(row, [source.page ? `p. ${source.page}` : null, source.source_text].filter(Boolean).join(' · ') || '—', 'cell-sub');
    referenceBody.appendChild(row);
  }
  toggleSection('secReferences', references.length > 0);

  // ── Lignes produit ──
  const lines = (b.lines?.length ? b.lines : (cleanOrder.line_items || [])).map(normalizedLine);
  const tbody = document.querySelector('#linesTable tbody');
  tbody.innerHTML = '';
  for (const l of lines) {
    const tr = document.createElement('tr');
    const commercialReferences = lineCommercialReferences(l);
    addTextCell(tr, l.line_number);
    addTextCell(tr, lineReferenceLabel(l, false), 'cell-primary');
    addTextCell(tr, l.description);
    addTextCell(tr, l.quantity != null ? fmt(l.quantity) : null);
    addTextCell(tr, l.uom);
    addTextCell(tr, unitPriceLabel(l, cur));
    addTextCell(tr, l.line_total != null ? money(l.line_total, l.currency || cur) : null);
    addTagCell(tr, [
      { label: 'Devis', values: commercialReferences.quotes },
      { label: 'Dérog.', values: commercialReferences.derogations, className: 'derogation' },
    ]);
    addTextCell(tr, confidenceLabel(l.confidence));
    tbody.appendChild(tr);
  }
  toggleSection('secLines', lines.length > 0);

  // ── Charges additionnelles ──
  const charges = (be.additional_charges?.length ? be.additional_charges
    : b.additional_charges?.length ? b.additional_charges
      : (cleanOrder.additional_charges || []));
  const cbody = document.querySelector('#chargesTable tbody');
  cbody.innerHTML = '';
  for (const ch of charges) {
    const tr = document.createElement('tr');
    const type = ch.charge_type || ch.type;
    addTextCell(tr, chargeTypeLabel(type));
    addTextCell(tr, ch.code || ch.reference || ch.supplier_reference, 'cell-primary');
    addTextCell(tr, ch.description);
    addTextCell(tr, ch.parent_line_number
      ? `Ligne ${ch.parent_line_number}${ch.parent_material_number ? ` · ${ch.parent_material_number}` : ''}`
      : 'Commande');
    addTextCell(tr, ch.quantity != null ? fmt(ch.quantity) : null);
    addTextCell(tr, ch.unit_price != null ? unitMoney(ch.unit_price, ch.currency || cur) : null);
    addTextCell(tr, ch.amount != null ? money(ch.amount, ch.currency || cur) : null);
    cbody.appendChild(tr);
  }
  toggleSection('secCharges', charges.length > 0);

  // ── Totaux ──
  const totEl = document.getElementById('totals');
  const chargeAmount = type => {
    const values = charges
      .filter(charge => (charge.charge_type || charge.type) === type)
      .map(charge => Number(charge.amount))
      .filter(Number.isFinite);
    return values.length ? values.reduce((sum, value) => sum + value, 0) : null;
  };
  fillKv(totEl, [
    ['Sous-total HT',    money(tot.subtotal,                              cur)],
    ['Remise',           money(tot.total_discount,                        cur)],
    ['Frais de port',    money(tot.total_shipping ?? tot.total_freight ?? chargeAmount('shipping'), cur)],
    ['Contributions environnementales', money(chargeAmount('environmental_fee'), cur)],
    ['Autres frais',     money(tot.total_surcharge ?? chargeAmount('surcharge'), cur)],
    ['TVA',              money(tot.total_vat       ?? tot.total_tax,      cur)],
    ['Total avant TVA',  money(tot.total_before_tax,                      cur)],
    ['Total HT',         money(tot.total_net,                             cur)],
    ['Net à payer',      money(tot.amount_due ?? tot.grand_total ?? tot.total_gross, cur)],
  ]);
  toggleSection('secTotals', totEl.children.length > 0);

  // ── Conditions commerciales ──
  const comEl = document.getElementById('commercial');
  fillKv(comEl, [
    ['Conditions paiement', com.payment_terms],
    ['Délai paiement',      com.payment_due_days != null ? `${com.payment_due_days} jours` : null],
    ['Mode paiement',       com.payment_method],
    ['Remise %',            com.discount_percent != null ? `${com.discount_percent} %` : null],
    ['Incoterm',            log.incoterm ? `${log.incoterm}${log.incoterm_location ? ' – ' + log.incoterm_location : ''}` : null],
    ['Livraison demandée',  log.requested_delivery_date],
    ['Mode transport',      log.transport_mode],
    ['Frais port',          money(com.freight_amount ?? com.shipping_cost, cur)],
  ]);
  toggleSection('secCommercial', comEl.children.length > 0);

  // ── JSON brut ──
  document.getElementById('jsonView').textContent = JSON.stringify(r, null, 2);
}
