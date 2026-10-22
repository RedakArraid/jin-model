'use strict';

pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdfjs-3.11.174/pdf.worker.min.js';

const state = {
  user: null,
  csrfToken: '',
  passwordMinLength: 8,
  catalog: [],
  documents: [],
  datasets: [],
  trainingRuns: [],
  users: [],
  auditEvents: [],
  activeDocument: null,
  fields: [],
  selectedField: -1,
  pdf: null,
  pdfUrl: '',
  page: 1,
  rendering: false,
  trainingPoll: null,
};

const $ = id => document.getElementById(id);
const loginPanel = $('loginPanel');
const studioApp = $('studioApp');
const loginError = $('loginError');
const usernameInput = $('usernameInput');
const passwordInput = $('passwordInput');
const connectionBadge = $('connectionBadge');
const documentList = $('documentList');
const documentCount = $('documentCount');
const annotationEmpty = $('annotationEmpty');
const annotationWorkspace = $('annotationWorkspace');
const fieldList = $('fieldList');
const annotationCanvas = $('annotationCanvas');
const pdfCanvas = $('pdfCanvas');

function hasPermission(permission) {
  const permissions = state.user?.permissions || [];
  return permissions.includes('*') || permissions.includes(permission);
}

async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrfToken) {
    headers['X-CSRF-Token'] = state.csrfToken;
  }
  const response = await fetch(`/api/studio${path}`, {
    ...options,
    credentials: 'same-origin',
    headers,
  });
  if (!response.ok) {
    let detail = `Erreur HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* response is not JSON */ }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response;
}

function showMessage(element, message, tone = '') {
  element.textContent = message;
  element.className = `inline-notice ${tone}`.trim();
  element.classList.toggle('hidden', !message);
}

function statusLabel(status) {
  return status === 'HUMAN_VALIDATED' ? 'Validé' : 'À annoter';
}

function valueText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function inferLabel(path) {
  const normalized = path.toLowerCase();
  const line = /(?:^|\.)(?:lines|line_items)\[\d+\]/.test(normalized);
  if (/document.*(?:type|class)/.test(normalized)) return 'DOCUMENT_TYPE';
  if (/purchase_order.*number|order_number/.test(normalized)) return 'ORDER_NUMBER';
  if (/purchase_order.*date|order_date/.test(normalized)) return 'ORDER_DATE';
  if (/customer_reference/.test(normalized)) return 'CUSTOMER_REFERENCE';
  if (/offer_number|quote_number/.test(normalized)) return 'OFFER_NUMBER';
  if (/derogation_number/.test(normalized)) return 'DEROGATION_NUMBER';
  if (line && /material_number|article_number|product_code|reference/.test(normalized)) return 'LINE_ITEM_REFERENCE';
  if (line && /description/.test(normalized)) return 'LINE_ITEM_DESCRIPTION';
  if (line && /quantity/.test(normalized)) return 'LINE_ITEM_QUANTITY';
  if (line && /(?:^|\.)(?:uom|unit)$/.test(normalized)) return 'LINE_ITEM_UNIT';
  if (line && /unit_price|net_unit_price/.test(normalized)) return 'LINE_ITEM_UNIT_PRICE';
  if (line && /line_total|amounts\.total|amounts\.net/.test(normalized)) return 'LINE_ITEM_TOTAL';
  if (/total.*(?:net|before_tax)|subtotal/.test(normalized)) return 'TOTAL_NET';
  if (/total.*vat|total_tax/.test(normalized)) return 'TOTAL_VAT';
  if (/total.*(?:gross|amount_due|grand)/.test(normalized)) return 'TOTAL_GROSS';
  const address = /delivery_address|addresses\[\d+\]/.test(normalized);
  if (address && /building/.test(normalized)) return 'ADDRESS_BUILDING';
  if (address && /house_number_suffix/.test(normalized)) return 'ADDRESS_HOUSE_NUMBER';
  if (address && /house_number/.test(normalized)) return 'ADDRESS_HOUSE_NUMBER';
  if (address && /street_type/.test(normalized)) return 'ADDRESS_STREET_TYPE';
  if (address && /street_name|address_line1|\.street$/.test(normalized)) return 'ADDRESS_STREET_NAME';
  if (address && /postal_code/.test(normalized)) return 'ADDRESS_POSTAL_CODE';
  if (address && /city/.test(normalized)) return 'ADDRESS_CITY';
  if (address && /cedex/.test(normalized)) return 'ADDRESS_CEDEX_NUMBER';
  if (address && /country_code|\.country$/.test(normalized)) return 'ADDRESS_COUNTRY_CODE';
  return '';
}

function normalizedEvidence(value) {
  const evidence = value && typeof value === 'object' ? value.evidence : null;
  if (!evidence || !Array.isArray(evidence.bbox) || evidence.bbox.length !== 4) return null;
  const space = String(evidence.bbox_space || evidence.coordinate_space || '').toLowerCase();
  if (!['normalized', 'layoutlm', '1000'].includes(space)) return null;
  return { page: Number(evidence.page || 1), bbox: evidence.bbox.map(Number) };
}

function extractFields(extraction) {
  const found = [];
  const seen = new Set();
  const ignored = new Set([
    'confidence', 'source', 'bbox', 'page', 'evidence', 'normalization', 'verification',
    'requires_review', 'raw_value', 'source_text', 'source_path', 'cell_id',
  ]);

  function add(path, label, value, evidence = null) {
    if (!label || seen.has(path) || value == null || value === '') return;
    seen.add(path);
    found.push({
      path,
      label,
      status: 'UNREVIEWED',
      extracted_value: value,
      corrected_value: null,
      evidence,
      comment: '',
    });
  }

  function visit(value, path) {
    if (value == null) return;
    if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, `${path}[${index}]`));
      return;
    }
    if (typeof value !== 'object') {
      add(path, inferLabel(path), value);
      return;
    }
    if (Object.prototype.hasOwnProperty.call(value, 'value')) {
      add(path, inferLabel(path), value.value, normalizedEvidence(value));
      return;
    }
    for (const [key, child] of Object.entries(value)) {
      if (ignored.has(key)) continue;
      visit(child, path ? `${path}.${key}` : key);
    }
  }

  visit(extraction, '');
  return found;
}

function catalogName(label) {
  return state.catalog.find(item => item.label === label)?.name || label;
}

async function enterStudio(user) {
  state.user = user;
  state.csrfToken = user.csrfToken || '';
  loginPanel.classList.add('hidden');
  studioApp.classList.remove('hidden');
  $('logoutButton').classList.remove('hidden');
  $('changePasswordButton').classList.remove('hidden');
  connectionBadge.textContent = user.displayName || user.username;
  connectionBadge.className = 'badge';
  $('reviewerIdentity').textContent = `${user.displayName || user.username} · ${user.username}`;
  applyPermissions();
  const catalog = await api('/field-catalog');
  state.catalog = catalog.fields || [];
  await Promise.all([loadDocuments(), loadDatasets(), loadTrainingRuns()]);
  if (hasPermission('users.manage')) await Promise.all([loadUsers(), loadAuditEvents()]);
  if (!state.trainingPoll) {
    state.trainingPoll = window.setInterval(() => {
      if (!document.hidden && state.user) loadTrainingRuns().catch(() => {});
    }, 10000);
  }
}

function applyPermissions() {
  const canAnnotate = hasPermission('annotations.draft');
  $('documentInput').parentElement.classList.toggle('hidden', !hasPermission('documents.write'));
  $('addFieldButton').classList.toggle('hidden', !canAnnotate);
  $('confirmAllButton').classList.toggle('hidden', !canAnnotate);
  $('saveDraftButton').classList.toggle('hidden', !canAnnotate);
  $('validateButton').classList.toggle('hidden', !hasPermission('annotations.validate'));
  $('createDatasetButton').classList.toggle('hidden', !hasPermission('datasets.write'));
  $('usersTab').classList.toggle('hidden', !hasPermission('users.manage'));
}

async function connect() {
  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  if (!username || !password) return;
  loginError.classList.add('hidden');
  try {
    const payload = await api('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    passwordInput.value = '';
    await enterStudio(payload.user);
  } catch (error) {
    loginError.textContent = error.message;
    loginError.classList.remove('hidden');
  }
}

async function restoreSession() {
  try {
    const payload = await api('/auth/me');
    await enterStudio(payload.user);
  } catch (_) {
    state.user = null;
    state.csrfToken = '';
    loginPanel.classList.remove('hidden');
  }
}

async function logout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch (_) { /* local cleanup still applies */ }
  state.user = null;
  state.csrfToken = '';
  if (state.trainingPoll) window.clearInterval(state.trainingPoll);
  state.trainingPoll = null;
  studioApp.classList.add('hidden');
  loginPanel.classList.remove('hidden');
  $('logoutButton').classList.add('hidden');
  $('changePasswordButton').classList.add('hidden');
  connectionBadge.textContent = 'Non connecté';
  connectionBadge.className = 'badge muted';
  passwordInput.value = '';
}

async function changeOwnPassword() {
  const currentPassword = window.prompt('Mot de passe actuel');
  if (currentPassword == null) return;
  const newPassword = window.prompt(`Nouveau mot de passe (${state.passwordMinLength} caractères minimum)`);
  if (newPassword == null) return;
  const confirmation = window.prompt('Confirmez le nouveau mot de passe');
  if (confirmation !== newPassword) {
    window.alert('Les mots de passe ne correspondent pas.');
    return;
  }
  if (newPassword.length < state.passwordMinLength) {
    window.alert(`Le mot de passe doit contenir au moins ${state.passwordMinLength} caractères.`);
    return;
  }
  try {
    await api('/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ currentPassword, newPassword }),
    });
    await logout();
    loginError.textContent = 'Mot de passe modifié. Reconnectez-vous.';
    loginError.classList.remove('hidden');
  } catch (error) {
    window.alert(error.message);
  }
}

async function loadDocuments() {
  const payload = await api('/documents');
  state.documents = payload.items || [];
  renderDocumentList();
}

function renderDocumentList() {
  const query = $('documentSearch').value.trim().toLowerCase();
  const status = $('documentStatusFilter').value;
  const visible = state.documents.filter(document => (
    (!query || document.filename.toLowerCase().includes(query) || document.documentId.includes(query))
    && (!status || document.status === status)
  ));
  documentList.replaceChildren();
  for (const item of visible) {
    const button = document.createElement('button');
    button.className = `document-item ${state.activeDocument?.documentId === item.documentId ? 'active' : ''}`;
    const name = document.createElement('strong');
    name.textContent = item.filename;
    const meta = document.createElement('div');
    meta.className = 'document-meta';
    const statusEl = document.createElement('span');
    statusEl.textContent = statusLabel(item.status);
    const date = document.createElement('span');
    date.textContent = String(item.updatedAt || '').slice(0, 10);
    meta.append(statusEl, date);
    button.append(name, meta);
    button.addEventListener('click', () => openDocument(item.documentId));
    documentList.appendChild(button);
  }
  documentCount.textContent = `${state.documents.length} document${state.documents.length > 1 ? 's' : ''}`;
}

async function uploadDocument(file) {
  if (!file) return;
  const uploadState = $('uploadState');
  showMessage(uploadState, 'Extraction JIN en cours…');
  try {
    const extractionForm = new FormData();
    extractionForm.append('file', file);
    const extractionResponse = await fetch('/api/extract?view=clean', { method: 'POST', body: extractionForm });
    if (!extractionResponse.ok) throw new Error(`Extraction JIN impossible (${extractionResponse.status})`);
    const extraction = await extractionResponse.json();

    showMessage(uploadState, 'Création du dossier d’annotation…');
    const form = new FormData();
    form.append('file', file);
    form.append('extraction_json', JSON.stringify(extraction));
    form.append('source_json', JSON.stringify({ source: 'jin_studio_upload', importedAt: new Date().toISOString() }));
    const created = await api('/documents', { method: 'POST', body: form });
    showMessage(uploadState, 'Document prêt à annoter.', 'success');
    await loadDocuments();
    await openDocument(created.documentId);
  } catch (error) {
    showMessage(uploadState, error.message, 'error');
  } finally {
    $('documentInput').value = '';
  }
}

async function openDocument(documentId) {
  const document = await api(`/documents/${encodeURIComponent(documentId)}`);
  state.activeDocument = document;
  state.fields = document.latestAnnotation?.field_annotations?.map(item => structuredClone(item))
    || extractFields(document.extraction || {});
  state.selectedField = state.fields.length ? 0 : -1;
  $('reviewerIdentity').textContent = `${state.user.displayName || state.user.username} · ${state.user.username}`;
  $('annotationNote').value = document.latestAnnotation?.note || '';
  $('annotationNote').disabled = !(hasPermission('annotations.draft') || hasPermission('annotations.validate'));
  $('activeFilename').textContent = document.filename;
  $('activeHash').textContent = `SHA-256 ${document.pdfSha256}`;
  $('activeStatus').textContent = statusLabel(document.status);
  $('activeStatus').className = `badge ${document.status === 'DRAFT' ? 'draft' : ''}`;
  annotationEmpty.classList.add('hidden');
  annotationWorkspace.classList.remove('hidden');
  renderDocumentList();
  renderFields();
  await loadPdf(documentId);
}

function populateLabelSelect(select, current) {
  select.replaceChildren();
  for (const field of state.catalog) {
    const option = document.createElement('option');
    option.value = field.label;
    option.textContent = field.name;
    option.selected = field.label === current;
    select.appendChild(option);
  }
}

function renderFields() {
  const editable = hasPermission('annotations.draft') || hasPermission('annotations.validate');
  fieldList.replaceChildren();
  state.fields.forEach((field, index) => {
    const node = $('fieldTemplate').content.firstElementChild.cloneNode(true);
    node.dataset.index = String(index);
    node.classList.toggle('selected', index === state.selectedField);
    node.classList.toggle('reviewed', ['CONFIRMED', 'CORRECTED'].includes(field.status));
    node.classList.toggle('problem', ['ABSENT', 'AMBIGUOUS'].includes(field.status));
    const label = node.querySelector('.field-label');
    populateLabelSelect(label, field.label);
    const path = node.querySelector('.field-path');
    const status = node.querySelector('.field-status');
    const extracted = node.querySelector('.extracted-value');
    const corrected = node.querySelector('.corrected-value');
    const page = node.querySelector('.evidence-page');
    const bbox = node.querySelector('.evidence-bbox');
    const comment = node.querySelector('.field-comment');
    path.value = field.path;
    status.value = field.status;
    extracted.value = valueText(field.extracted_value);
    corrected.value = valueText(field.corrected_value);
    corrected.disabled = field.status !== 'CORRECTED';
    page.value = field.evidence?.page || '';
    bbox.value = field.evidence?.bbox?.join(',') || '';
    comment.value = field.comment || '';
    label.disabled = !editable;
    path.disabled = !editable;
    status.disabled = !editable;
    corrected.disabled = !editable || field.status !== 'CORRECTED';
    page.disabled = !editable;
    bbox.disabled = !editable;
    comment.disabled = !editable;
    node.addEventListener('click', () => {
      if (state.selectedField === index) return;
      state.selectedField = index;
      fieldList.querySelectorAll('.field-card').forEach((item, itemIndex) => {
        item.classList.toggle('selected', itemIndex === index);
      });
      drawAnnotations();
    });
    label.addEventListener('change', event => { field.label = event.target.value; });
    path.addEventListener('input', event => { field.path = event.target.value; });
    status.addEventListener('change', event => {
      field.status = event.target.value;
      corrected.disabled = !editable || field.status !== 'CORRECTED';
      renderProgress();
      node.classList.toggle('reviewed', ['CONFIRMED', 'CORRECTED'].includes(field.status));
      node.classList.toggle('problem', ['ABSENT', 'AMBIGUOUS'].includes(field.status));
    });
    corrected.addEventListener('input', event => { field.corrected_value = event.target.value; });
    page.addEventListener('input', () => updateEvidence(field, page.value, bbox.value));
    bbox.addEventListener('input', () => updateEvidence(field, page.value, bbox.value));
    comment.addEventListener('input', event => { field.comment = event.target.value; });
    fieldList.appendChild(node);
  });
  renderProgress();
  drawAnnotations();
}

function updateEvidence(field, pageValue, bboxValue) {
  const box = String(bboxValue || '').split(',').map(value => Number(value.trim()));
  if (box.length === 4 && box.every(Number.isFinite) && Number(pageValue) > 0) {
    field.evidence = { page: Number(pageValue), bbox: box };
  } else {
    field.evidence = null;
  }
  drawAnnotations();
}

function renderProgress() {
  const reviewed = state.fields.filter(field => field.status !== 'UNREVIEWED').length;
  $('reviewProgress').textContent = `${reviewed}/${state.fields.length} champs vérifiés`;
}

async function loadPdf(documentId) {
  if (state.pdfUrl) URL.revokeObjectURL(state.pdfUrl);
  const response = await api(`/documents/${encodeURIComponent(documentId)}/pdf`);
  const blob = await response.blob();
  state.pdfUrl = URL.createObjectURL(blob);
  state.pdf = await pdfjsLib.getDocument(state.pdfUrl).promise;
  state.page = 1;
  $('pageCount').textContent = String(state.pdf.numPages);
  await renderPdfPage();
}

async function renderPdfPage() {
  if (!state.pdf || state.rendering) return;
  state.rendering = true;
  try {
    const page = await state.pdf.getPage(state.page);
    const base = page.getViewport({ scale: 1 });
    const available = Math.max(360, $('pdfCanvasWrap').clientWidth - 40);
    const scale = Math.min(1.7, Math.max(.7, available / base.width));
    const viewport = page.getViewport({ scale });
    const context = pdfCanvas.getContext('2d');
    pdfCanvas.width = viewport.width;
    pdfCanvas.height = viewport.height;
    annotationCanvas.width = viewport.width;
    annotationCanvas.height = viewport.height;
    await page.render({ canvasContext: context, viewport }).promise;
    $('currentPage').textContent = String(state.page);
    drawAnnotations();
  } finally {
    state.rendering = false;
  }
}

function drawAnnotations(preview = null) {
  const context = annotationCanvas.getContext('2d');
  context.clearRect(0, 0, annotationCanvas.width, annotationCanvas.height);
  state.fields.forEach((field, index) => {
    if (Number(field.evidence?.page) !== state.page || !field.evidence?.bbox) return;
    const [x0, y0, x1, y1] = field.evidence.bbox;
    context.strokeStyle = index === state.selectedField ? '#2356d8' : '#0b8c65';
    context.lineWidth = index === state.selectedField ? 3 : 1.5;
    context.fillStyle = index === state.selectedField ? 'rgba(35,86,216,.12)' : 'rgba(11,140,101,.08)';
    const x = x0 / 1000 * annotationCanvas.width;
    const y = y0 / 1000 * annotationCanvas.height;
    const width = (x1 - x0) / 1000 * annotationCanvas.width;
    const height = (y1 - y0) / 1000 * annotationCanvas.height;
    context.fillRect(x, y, width, height);
    context.strokeRect(x, y, width, height);
    context.fillStyle = index === state.selectedField ? '#2356d8' : '#0b8c65';
    context.font = '11px sans-serif';
    context.fillText(catalogName(field.label), x + 3, Math.max(11, y - 3));
  });
  if (preview) {
    context.strokeStyle = '#e35d10';
    context.lineWidth = 2;
    context.setLineDash([5, 4]);
    context.strokeRect(preview.x, preview.y, preview.width, preview.height);
    context.setLineDash([]);
  }
}

let drawStart = null;
annotationCanvas.addEventListener('pointerdown', event => {
  if (state.selectedField < 0 || !(hasPermission('annotations.draft') || hasPermission('annotations.validate'))) return;
  const rect = annotationCanvas.getBoundingClientRect();
  drawStart = { x: event.clientX - rect.left, y: event.clientY - rect.top };
  annotationCanvas.setPointerCapture(event.pointerId);
});
annotationCanvas.addEventListener('pointermove', event => {
  if (!drawStart) return;
  const rect = annotationCanvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  drawAnnotations({
    x: Math.min(drawStart.x, x), y: Math.min(drawStart.y, y),
    width: Math.abs(x - drawStart.x), height: Math.abs(y - drawStart.y),
  });
});
annotationCanvas.addEventListener('pointerup', event => {
  if (!drawStart || state.selectedField < 0) return;
  const rect = annotationCanvas.getBoundingClientRect();
  const end = { x: event.clientX - rect.left, y: event.clientY - rect.top };
  const x0 = Math.min(drawStart.x, end.x);
  const y0 = Math.min(drawStart.y, end.y);
  const x1 = Math.max(drawStart.x, end.x);
  const y1 = Math.max(drawStart.y, end.y);
  drawStart = null;
  if (x1 - x0 < 5 || y1 - y0 < 5) return drawAnnotations();
  state.fields[state.selectedField].evidence = {
    page: state.page,
    bbox: [x0, y0, x1, y1].map((value, index) => Math.round(
      value / (index % 2 === 0 ? annotationCanvas.width : annotationCanvas.height) * 1000,
    )),
  };
  renderFields();
});

function annotationPayload(status) {
  return {
    status,
    note: $('annotationNote').value.trim(),
    field_annotations: state.fields.map(field => ({
      path: field.path,
      label: field.label,
      status: field.status,
      extracted_value: field.extracted_value,
      corrected_value: field.status === 'CORRECTED' ? field.corrected_value : null,
      evidence: field.evidence || null,
      comment: field.comment || '',
    })),
  };
}

async function saveAnnotation(status) {
  if (!state.activeDocument) return;
  const message = $('annotationMessage');
  showMessage(message, status === 'DRAFT' ? 'Enregistrement…' : 'Validation…');
  try {
    await api(`/documents/${encodeURIComponent(state.activeDocument.documentId)}/annotation`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(annotationPayload(status)),
    });
    showMessage(message, status === 'DRAFT' ? 'Brouillon enregistré.' : 'Annotation humaine validée.', 'success');
    await loadDocuments();
    await openDocument(state.activeDocument.documentId);
    await loadDatasets();
  } catch (error) {
    showMessage(message, error.message, 'error');
  }
}

async function loadDatasets() {
  const payload = await api('/datasets');
  state.datasets = payload.items || [];
  renderDatasets();
  renderTraining();
}

function renderDatasets() {
  const container = $('datasetList');
  container.replaceChildren();
  if (!state.datasets.length) {
    const empty = document.createElement('p'); empty.className = 'muted-text'; empty.textContent = 'Aucun dataset créé.'; container.appendChild(empty); return;
  }
  for (const dataset of state.datasets) {
    const card = document.createElement('article'); card.className = 'info-card';
    const title = document.createElement('h3'); title.textContent = `${dataset.name} · v${dataset.version}`;
    const badge = document.createElement('span'); badge.className = `badge ${dataset.status === 'READY' ? '' : 'draft'}`; badge.textContent = dataset.status === 'READY' ? 'Prêt' : 'Brouillon';
    const stats = document.createElement('dl');
    [['Documents', dataset.documentCount], ['Zones revues', dataset.regionCount], ['Labels', (dataset.labels || []).length]].forEach(([name, value]) => {
      const dt = document.createElement('dt'); dt.textContent = name; const dd = document.createElement('dd'); dd.textContent = String(value); stats.append(dt, dd);
    });
    card.append(title, badge, stats);
    if (dataset.warnings?.length) {
      const warnings = document.createElement('ul'); warnings.className = 'warning-list';
      dataset.warnings.forEach(text => { const item = document.createElement('li'); item.textContent = text; warnings.appendChild(item); });
      card.appendChild(warnings);
    }
    container.appendChild(card);
  }
}

async function createDataset() {
  const validated = state.documents.filter(document => document.status === 'HUMAN_VALIDATED');
  if (!validated.length) return showMessage($('datasetMessage'), 'Aucun document validé n’est disponible.', 'error');
  const name = window.prompt('Nom du dataset versionné', 'commandes-revues');
  if (!name) return;
  try {
    const dataset = await api('/datasets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, documentIds: validated.map(document => document.documentId) }),
    });
    showMessage($('datasetMessage'), `Dataset ${dataset.name} v${dataset.version} créé.`, 'success');
    await loadDatasets();
  } catch (error) { showMessage($('datasetMessage'), error.message, 'error'); }
}

async function loadTrainingRuns() {
  const payload = await api('/training-runs');
  state.trainingRuns = payload.items || [];
  renderTraining();
}

function renderTraining() {
  const container = $('trainingList');
  container.replaceChildren();
  for (const dataset of state.datasets.filter(item => item.status === 'READY')) {
    const card = document.createElement('article'); card.className = 'info-card';
    const title = document.createElement('h3'); title.textContent = `Nouveau candidat · ${dataset.name} v${dataset.version}`;
    const text = document.createElement('p'); text.className = 'muted-text'; text.textContent = `${dataset.documentCount} documents, ${dataset.regionCount} zones humaines.`;
    card.append(title, text);
    if (hasPermission('training.write')) {
      const button = document.createElement('button'); button.className = 'button primary'; button.textContent = 'Lancer l’entraînement';
      button.addEventListener('click', () => prepareTraining(dataset.datasetId));
      card.appendChild(button);
    }
    container.appendChild(card);
  }
  for (const run of state.trainingRuns) {
    const card = document.createElement('article'); card.className = 'info-card';
    const title = document.createElement('h3'); title.textContent = `${run.modelFamily} · ${run.status}`;
    const details = [run.runId, String(run.createdAt).slice(0, 19)];
    if (run.currentStep) details.push(`Étape : ${run.currentStep}`);
    const meta = document.createElement('p'); meta.className = 'muted-text'; meta.textContent = details.join(' · ');
    const command = document.createElement('pre'); command.className = 'command'; command.textContent = run.command;
    card.append(title, meta, command);
    if (run.error) {
      const error = document.createElement('p'); error.className = 'inline-notice error'; error.textContent = run.error;
      card.appendChild(error);
    }
    container.appendChild(card);
  }
  if (!container.children.length) {
    const empty = document.createElement('p'); empty.className = 'muted-text'; empty.textContent = 'Aucun dataset prêt et aucun entraînement lancé.'; container.appendChild(empty);
  }
}

async function prepareTraining(datasetId) {
  try {
    const run = await api('/training-runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ datasetId, modelFamily: 'layoutlmv3', baseModel: 'microsoft/layoutlmv3-base' }),
    });
    showMessage($('trainingMessage'), `Entraînement ${run.runId} ajouté à la file.`, 'success');
    await loadTrainingRuns();
  } catch (error) { showMessage($('trainingMessage'), error.message, 'error'); }
}

const roleLabels = {
  VIEWER: 'Lecture',
  ANNOTATOR: 'Annotation',
  VALIDATOR: 'Validation',
  TRAINER: 'Entraînement',
  ADMIN: 'Administration',
};

async function loadUsers() {
  const payload = await api('/users');
  state.users = payload.items || [];
  renderUsers();
}

function renderUsers() {
  const container = $('userList');
  container.replaceChildren();
  for (const user of state.users) {
    const card = document.createElement('article');
    card.className = `user-card ${user.active ? '' : 'inactive'}`;
    const identity = document.createElement('div');
    identity.className = 'user-identity';
    const name = document.createElement('input');
    name.value = user.displayName;
    name.maxLength = 120;
    name.setAttribute('aria-label', `Nom affiché de ${user.username}`);
    const username = document.createElement('strong');
    username.textContent = user.username;
    const lastLogin = document.createElement('span');
    lastLogin.className = 'muted-text';
    lastLogin.textContent = user.lastLoginAt ? `Dernière connexion : ${String(user.lastLoginAt).slice(0, 16).replace('T', ' ')}` : 'Jamais connecté';
    identity.append(name, username, lastLogin);

    const roles = document.createElement('div');
    roles.className = 'role-picker compact-roles';
    for (const [code, labelText] of Object.entries(roleLabels)) {
      const label = document.createElement('label');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = code;
      checkbox.checked = user.roles.includes(code);
      checkbox.dataset.role = code;
      label.append(checkbox, document.createTextNode(labelText));
      roles.appendChild(label);
    }

    const actions = document.createElement('div');
    actions.className = 'user-actions';
    const activeLabel = document.createElement('label');
    const active = document.createElement('input');
    active.type = 'checkbox';
    active.checked = user.active;
    activeLabel.append(active, document.createTextNode('Compte actif'));
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'button primary compact';
    save.textContent = 'Enregistrer';
    save.addEventListener('click', async () => {
      const selectedRoles = [...roles.querySelectorAll('input:checked')].map(input => input.value);
      try {
        await api(`/users/${encodeURIComponent(user.userId)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ displayName: name.value.trim(), roles: selectedRoles, active: active.checked }),
        });
        showMessage($('userMessage'), `Compte ${user.username} mis à jour.`, 'success');
        await loadUsers();
        await loadAuditEvents();
      } catch (error) { showMessage($('userMessage'), error.message, 'error'); }
    });
    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'button secondary compact';
    reset.textContent = 'Changer le mot de passe';
    reset.addEventListener('click', () => resetPassword(user));
    actions.append(activeLabel, save, reset);
    card.append(identity, roles, actions);
    container.appendChild(card);
  }
}

async function createUser(event) {
  event.preventDefault();
  const roles = [...document.querySelectorAll('input[name="newUserRole"]:checked')].map(input => input.value);
  try {
    const user = await api('/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: $('newUsername').value.trim(),
        displayName: $('newDisplayName').value.trim(),
        password: $('newPassword').value,
        roles,
      }),
    });
    $('createUserForm').reset();
    document.querySelector('input[name="newUserRole"][value="ANNOTATOR"]').checked = true;
    showMessage($('userMessage'), `Compte ${user.username} créé.`, 'success');
    await loadUsers();
    await loadAuditEvents();
  } catch (error) { showMessage($('userMessage'), error.message, 'error'); }
}

async function resetPassword(user) {
  const password = window.prompt(`Nouveau mot de passe pour ${user.username}`);
  if (password == null) return;
  const confirmation = window.prompt('Confirmez le nouveau mot de passe');
  if (confirmation !== password) {
    return showMessage($('userMessage'), 'Les mots de passe ne correspondent pas.', 'error');
  }
  if (password.length < state.passwordMinLength) {
    return showMessage($('userMessage'), `Le mot de passe doit contenir au moins ${state.passwordMinLength} caractères.`, 'error');
  }
  try {
    await api(`/users/${encodeURIComponent(user.userId)}/password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (user.userId === state.user.userId) {
      await logout();
      loginError.textContent = 'Mot de passe modifié. Reconnectez-vous.';
      loginError.classList.remove('hidden');
      return;
    }
    showMessage($('userMessage'), `Mot de passe de ${user.username} modifié.`, 'success');
    await loadAuditEvents();
  } catch (error) { showMessage($('userMessage'), error.message, 'error'); }
}

async function loadAuditEvents() {
  const payload = await api('/audit-events?limit=200');
  state.auditEvents = payload.items || [];
  const container = $('auditList');
  container.replaceChildren();
  for (const event of state.auditEvents) {
    const row = document.createElement('div');
    row.className = 'audit-row';
    const when = document.createElement('time');
    when.textContent = String(event.createdAt || '').slice(0, 19).replace('T', ' ');
    const actor = document.createElement('strong');
    actor.textContent = event.actorUsername;
    const action = document.createElement('span');
    action.textContent = event.action;
    const target = document.createElement('span');
    target.className = 'muted-text';
    target.textContent = [event.targetType, event.targetId].filter(Boolean).join(' · ');
    row.append(when, actor, action, target);
    container.appendChild(row);
  }
  if (!container.children.length) {
    const empty = document.createElement('p');
    empty.className = 'muted-text';
    empty.textContent = 'Aucune activité enregistrée.';
    container.appendChild(empty);
  }
}

$('connectButton').addEventListener('click', connect);
passwordInput.addEventListener('keydown', event => { if (event.key === 'Enter') connect(); });
$('logoutButton').addEventListener('click', logout);
$('changePasswordButton').addEventListener('click', changeOwnPassword);
$('createUserForm').addEventListener('submit', createUser);
$('refreshAuditButton').addEventListener('click', () => loadAuditEvents().catch(error => showMessage($('userMessage'), error.message, 'error')));
$('documentInput').addEventListener('change', event => uploadDocument(event.target.files?.[0]));
$('documentSearch').addEventListener('input', renderDocumentList);
$('documentStatusFilter').addEventListener('change', renderDocumentList);
$('confirmAllButton').addEventListener('click', () => {
  state.fields.forEach(field => { if (field.status === 'UNREVIEWED') field.status = 'CONFIRMED'; });
  renderFields();
});
$('addFieldButton').addEventListener('click', () => {
  const index = state.fields.length;
  state.fields.push({ path: `manual.field[${index}]`, label: state.catalog[0]?.label || 'DOCUMENT_TYPE', status: 'UNREVIEWED', extracted_value: '', corrected_value: null, evidence: null, comment: '' });
  state.selectedField = index;
  renderFields();
});
$('saveDraftButton').addEventListener('click', () => saveAnnotation('DRAFT'));
$('validateButton').addEventListener('click', () => saveAnnotation('HUMAN_VALIDATED'));
$('createDatasetButton').addEventListener('click', createDataset);
$('previousPageButton').addEventListener('click', async () => { if (state.page > 1) { state.page -= 1; await renderPdfPage(); } });
$('nextPageButton').addEventListener('click', async () => { if (state.pdf && state.page < state.pdf.numPages) { state.page += 1; await renderPdfPage(); } });

document.querySelectorAll('.section-tab').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.section-tab').forEach(item => item.classList.toggle('active', item === button));
  document.querySelectorAll('.studio-section').forEach(section => section.classList.add('hidden'));
  $(`${button.dataset.section}Section`).classList.remove('hidden');
}));

api('/auth/config').then(config => {
  state.passwordMinLength = Number(config.passwordMinLength || 8);
  $('newPassword').minLength = state.passwordMinLength;
  if (!config.hasUsers) {
    $('setupHint').textContent = 'Aucun compte n’existe encore. Un administrateur doit créer le premier compte avec la commande indiquée dans la documentation.';
    $('setupHint').classList.remove('hidden');
  }
}).catch(() => {});
restoreSession();
