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

const lineReferenceLabel = line => {
  const primary = line.material_number || line.article_number || line.product_code
    || line.manufacturer_part_number || line.supplier_material_number;
  const details = [];
  if (line.supplier_material_number && line.supplier_material_number !== primary) {
    details.push(`Fourn. ${line.supplier_material_number}`);
  }
  if (line.customer_material_number && line.customer_material_number !== primary) {
    details.push(`Client ${line.customer_material_number}`);
  }
  return [primary, ...details].filter(Boolean).join(' · ') || null;
};

function uiDeliveryAddresses(result) {
  const normalize = value => String(value || '').normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '').replace(/[^A-Z0-9]+/gi, ' ').trim().toUpperCase();
  const withoutDuplicateRecipient = (values, partyName) => {
    const lines = [];
    for (const value of values || []) {
      const text = String(value || '').trim();
      if (!text || lines.some(existing => normalize(existing) === normalize(text))) continue;
      lines.push(text);
    }
    if (lines.length && partyName && normalize(lines[0]) === normalize(partyName)) lines.shift();
    return lines;
  };
  const normalized = result?.normalized_output?.order?.delivery_address;
  if (normalized) {
    const partyName = normalized.party_name || normalized.components?.recipient || '';
    const rawLines = normalized.formatted_lines?.length
      ? normalized.formatted_lines
      : normalized.formatted ? [normalized.formatted] : [];
    const lines = withoutDuplicateRecipient(rawLines, partyName);
    if (lines.length) return [{
      role: normalized.role || 'ship_to',
      role_label: normalized.role_label || 'Adresse de livraison sélectionnée',
      party_name: partyName,
      customer_agency_code: normalized.customer_agency_code || '',
      lines,
      role_confidence: normalized.role_confidence,
      address_confidence: normalized.address_confidence,
      clean_status: normalized.normalization?.status,
      verification: normalized.verification || {},
      evidence: normalized.evidence || {},
      selected: true,
    }];
  }

  const be = result?.business_extractions || {};
  const order = be.purchase_order || {};
  const addresses = be.business_addresses?.length
    ? be.business_addresses : (order.business_addresses || []);
  return addresses.flatMap(address => {
    const role = String(address.role || '').toLowerCase();
    const warnings = address.warnings || [];
    if (!['ship_to', 'deliver_to', 'consignee'].includes(role)
        || /superseded/i.test(String(address.role_label || ''))
        || warnings.some(warning => /superseded/i.test(String(warning)))) return [];
    const clean = address.clean_address || {};
    const rawLines = clean.lines?.length
      ? clean.lines
      : address.formatted_address_clean ? [address.formatted_address_clean] : [];
    const partyName = address.party_name || clean.components?.recipient || '';
    const lines = withoutDuplicateRecipient(rawLines, partyName);
    if (!lines.length) return [];
    return [{
      role,
      role_label: address.role_label || 'Adresse de livraison',
      party_name: partyName,
      customer_agency_code: address.customer_agency_code || '',
      lines,
      contact_name: address.contact_name,
      contact_email: address.contact_email,
      contact_phone: address.contact_phone,
      role_confidence: address.role_confidence,
      address_confidence: address.address_confidence,
      clean_status: clean.status,
      verification: address.ban_verification || address.address_verification || {},
      evidence: address.evidence || {},
      selected: false,
    }];
  });
}

// ── Health ─────────────────────────────────────────────────────────────────
async function health() {
  const el = document.getElementById('health');
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    el.className = 'health ok';
    el.textContent = `API OK · ${j.version}`;
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

  if      (entry.status === 'processing') { stateProcessing.classList.remove('hidden'); }
  else if (entry.status === 'error')      { stateError.textContent = entry.error; stateError.classList.remove('hidden'); }
  else if (entry.status === 'pending')    { statePending.classList.remove('hidden'); }
  else if (entry.status === 'done')       { render(entry.result); resultsEl.classList.remove('hidden'); downloadBtn.disabled = false; }
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
  const tot = b.totals           || {};
  const com = b.commercial       || {};
  const log = b.logistics        || {};
  const cur = pick(po, ['currency.value']) || tot.currency || 'EUR';

  // ── Metrics bar ──
  fillMetrics([
    ['Type',          pick(r, ['document.detected_document_type', 'detected_document_type'])],
    ['Score technique', pick(r, ['quality.overall_confidence',    'overall_confidence'])],
    ['Validation',    pick(b, ['validation.status',              'validation_status'])],
    ['Revue humaine', pick(r, ['quality.requires_human_review',  'requires_human_review'], false) ? 'OUI' : 'NON'],
    ['Pages',         pick(r, ['document.page_count',            'page_count'])],
    ['Version',       pick(r, ['document.engine_version',        'engine_version'])],
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
    ['N° devis',          pick(po, ['quote_number.value',       'quote_number'])],
    ['N° projet',         pick(po, ['project_number.value',     'project_number'])],
  ]);
  toggleSection('secSummary', summaryEl.children.length > 0);

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

  // ── Lignes produit ──
  const lines = b.lines || [];
  const tbody = document.querySelector('#linesTable tbody');
  tbody.innerHTML = '';
  for (const l of lines) {
    const tr = document.createElement('tr');
    [ l.line_number,
      lineReferenceLabel(l),
      l.description,
      l.quantity,
      l.uom,
      unitPriceLabel(l, cur),
      l.line_total  != null ? money(l.line_total,   l.currency || cur) : null,
      l.confidence  != null ? fmt(l.confidence) : null,
    ].forEach(v => {
      const td = document.createElement('td');
      td.textContent = v ?? '—';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  toggleSection('secLines', lines.length > 0);

  // ── Charges additionnelles ──
  const charges = be.additional_charges || b.additional_charges || [];
  const cbody = document.querySelector('#chargesTable tbody');
  cbody.innerHTML = '';
  for (const ch of charges) {
    const tr = document.createElement('tr');
    [ ch.charge_type,
      ch.description,
      ch.supplier_reference || ch.code,
      ch.quantity != null ? fmt(ch.quantity) : null,
      ch.unit_price != null ? unitMoney(ch.unit_price, ch.currency || cur) : null,
      ch.amount     != null ? money(ch.amount,     ch.currency || cur) : null,
    ].forEach(v => {
      const td = document.createElement('td');
      td.textContent = v ?? '—';
      tr.appendChild(td);
    });
    cbody.appendChild(tr);
  }
  toggleSection('secCharges', charges.length > 0);

  // ── Totaux ──
  const totEl = document.getElementById('totals');
  fillKv(totEl, [
    ['Sous-total HT',    money(tot.subtotal,                              cur)],
    ['Remise',           money(tot.total_discount,                        cur)],
    ['Surcharges',       money(tot.total_surcharge ?? tot.total_freight,  cur)],
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
