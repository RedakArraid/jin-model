'use strict';

pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdfjs-3.11.174/pdf.worker.min.js';

const DOCUMENT_TYPE_OPTIONS = [
  ['purchase_order', 'Bon de commande'],
  ['purchase_order_with_terms', 'Bon de commande avec conditions'],
  ['purchase_order_bundle', 'Regroupement de commandes'],
  ['request_for_quotation', 'Demande de prix'],
  ['quotation', 'Devis / offre'],
  ['order_confirmation', 'Confirmation de commande'],
  ['order_cancellation', 'Annulation de commande'],
  ['invoice', 'Facture'],
  ['credit_note', 'Avoir'],
  ['delivery_note', 'Bon de livraison'],
  ['contract', 'Contrat'],
  ['legal_terms', 'Conditions contractuelles'],
  ['generic_document', 'Document générique'],
  ['__other__', 'Autre…'],
];
const DOCUMENT_TYPE_VALUES = new Set(
  DOCUMENT_TYPE_OPTIONS.filter(([value]) => value !== '__other__').map(([value]) => value),
);
const lineItemTools = globalThis.JinStudioLineItems;
if (!lineItemTools) throw new Error('Le module de gestion des lignes de commande est absent.');
const MIN_ZOOM = .5;
const MAX_ZOOM = 4;
const ZOOM_STEP = .25;
const OTHER_ANNOTATIONS_VISIBILITY_KEY = 'jin-studio.show-other-annotations';

function storedOtherAnnotationsVisibility() {
  try {
    return window.localStorage.getItem(OTHER_ANNOTATIONS_VISIBILITY_KEY) !== 'false';
  } catch (_) {
    return true;
  }
}

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
  pendingRender: false,
  zoom: 1,
  showOtherAnnotations: storedOtherAnnotationsVisibility(),
  pageTextItems: [],
  regionRequest: 0,
  dirty: false,
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
const canvasStage = $('canvasStage');
const showOtherAnnotationsToggle = $('showOtherAnnotationsToggle');
showOtherAnnotationsToggle.checked = state.showOtherAnnotations;

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

function inferLabel(path, explicitRole = '') {
  const normalized = path.toLowerCase();
  const line = /(?:^|\.)(?:lines|line_items)\[\d+\]/.test(normalized);
  const roleMatch = normalized.match(/(?:^|\.)(buyer|supplier|ship_to|deliver_to|delivery_address|bill_to|invoice_to|payer|ship_from)(?:\.|\[|$)/);
  const role = explicitRole || (roleMatch ? {
    buyer: 'BUYER', supplier: 'SUPPLIER', ship_to: 'SHIP_TO',
    deliver_to: 'SHIP_TO', delivery_address: 'SHIP_TO', bill_to: 'BILL_TO',
    invoice_to: 'BILL_TO', payer: 'PAYER', ship_from: 'SHIP_FROM',
  }[roleMatch[1]] : '');
  const component = normalized.split('.').pop().replace(/\[\d+\]$/, '');
  if (/document.*(?:type|class)/.test(normalized)) return 'DOCUMENT_TYPE';
  if (/purchase_order.*number|order_number/.test(normalized)) return 'ORDER_NUMBER';
  if (/purchase_order.*date|order_date/.test(normalized)) return 'ORDER_DATE';
  if (/customer_reference/.test(normalized)) return 'CUSTOMER_REFERENCE';
  if (/customer_agency_code|customer_code/.test(normalized)) return 'CUSTOMER_CODE';
  if (!line && /requested_delivery_date|expected_delivery_date|required_date|delivery_date/.test(normalized)) return 'DELIVERY_DATE';
  if (/offer_number|quote_number/.test(normalized)) return 'OFFER_NUMBER';
  if (/derogation_number/.test(normalized)) return 'DEROGATION_NUMBER';
  if (line && /line_number/.test(normalized)) return 'LINE_ITEM_NUMBER';
  if (line && /material_number|article_number|product_code|reference/.test(normalized)) return 'LINE_ITEM_REFERENCE';
  if (line && /description/.test(normalized)) return 'LINE_ITEM_DESCRIPTION';
  if (line && /quantity/.test(normalized)) return 'LINE_ITEM_QUANTITY';
  if (line && /(?:^|\.)(?:uom|unit)$/.test(normalized)) return 'LINE_ITEM_UNIT';
  if (line && /unit_price|net_unit_price/.test(normalized)) return 'LINE_ITEM_UNIT_PRICE';
  if (line && /line_total|amounts\.total|amounts\.net/.test(normalized)) return 'LINE_ITEM_TOTAL';
  if (line && /requested_delivery_date|delivery_date/.test(normalized)) return 'LINE_ITEM_DELIVERY_DATE';
  if (/additional_charges\[\d+\]/.test(normalized) && /amount|\.net|\.total/.test(normalized)) return 'ADDITIONAL_CHARGE_AMOUNT';
  if (/additional_charges\[\d+\]/.test(normalized) && /description|label|charge_type|\.type/.test(normalized)) return 'ADDITIONAL_CHARGE_LABEL';
  if (/totals.*subtotal/.test(normalized)) return 'TOTAL_SUBTOTAL';
  if (/total.*(?:net|before_tax)|subtotal/.test(normalized)) return 'TOTAL_NET';
  if (/total.*vat|total_tax/.test(normalized)) return 'TOTAL_VAT';
  if (/totals.*amount_due/.test(normalized)) return 'TOTAL_AMOUNT_DUE';
  if (/total.*(?:gross|grand)/.test(normalized)) return 'TOTAL_GROSS';
  if (role && /(?:name|legal_name|party_name|recipient)$/.test(component)) return `${role}_NAME`;
  if (role && /(?:code|party_code)$/.test(component)) return `${role}_CODE`;
  if (role && component === 'vat_number') return `${role}_VAT_NUMBER`;
  if (role && component === 'siren') return `${role}_SIREN`;
  if (role && component === 'siret') return `${role}_SIRET`;
  if (role && ['phone', 'fax', 'email'].includes(component)) return `${role}_CONTACT_${component.toUpperCase()}`;
  const roleAddressComponents = {
    building: 'BUILDING', residence: 'RESIDENCE', entry: 'ENTRY', floor: 'FLOOR',
    unit: 'UNIT', house_number: 'HOUSE_NUMBER', house_number_suffix: 'HOUSE_NUMBER_SUFFIX',
    street_type: 'STREET_TYPE', street_name: 'STREET_NAME', street: 'STREET',
    industrial_zone: 'INDUSTRIAL_ZONE', activity_park: 'ACTIVITY_PARK',
    place_name: 'PLACE_NAME', postal_box: 'POSTAL_BOX', tsa: 'TSA', cs: 'CS',
    postal_routing_code: 'POSTAL_ROUTING_CODE', postal_code: 'POSTAL_CODE',
    city: 'CITY', cedex_number: 'CEDEX_NUMBER', country: 'COUNTRY',
    country_code: 'COUNTRY_CODE',
  };
  if (role && roleAddressComponents[component] && /address|components/.test(normalized)) {
    return `${role}_${roleAddressComponents[component]}`;
  }
  if (!role && component === 'vat_number') return 'VAT_NUMBER';
  if (!role && component === 'siren') return 'SIREN';
  if (!role && component === 'siret') return 'SIRET';
  const address = /delivery_address|addresses\[\d+\]/.test(normalized);
  if (address && /building/.test(normalized)) return 'ADDRESS_BUILDING';
  if (address && /residence/.test(normalized)) return 'ADDRESS_RESIDENCE';
  if (address && /entry/.test(normalized)) return 'ADDRESS_ENTRY';
  if (address && /floor/.test(normalized)) return 'ADDRESS_FLOOR';
  if (address && /(?:^|\.)unit$/.test(normalized)) return 'ADDRESS_UNIT';
  if (address && /house_number_suffix/.test(normalized)) return 'ADDRESS_HOUSE_NUMBER_SUFFIX';
  if (address && /house_number/.test(normalized)) return 'ADDRESS_HOUSE_NUMBER';
  if (address && /street_type/.test(normalized)) return 'ADDRESS_STREET_TYPE';
  if (address && /street_name|address_line1|\.street$/.test(normalized)) return 'ADDRESS_STREET_NAME';
  if (address && /industrial_zone/.test(normalized)) return 'ADDRESS_INDUSTRIAL_ZONE';
  if (address && /activity_park/.test(normalized)) return 'ADDRESS_ACTIVITY_PARK';
  if (address && /place_name/.test(normalized)) return 'ADDRESS_PLACE_NAME';
  if (address && /postal_box/.test(normalized)) return 'ADDRESS_POSTAL_BOX';
  if (address && /(?:^|\.)tsa$/.test(normalized)) return 'ADDRESS_TSA';
  if (address && /postal_routing_code/.test(normalized)) return 'ADDRESS_POSTAL_ROUTING_CODE';
  if (address && /(?:^|\.)cs$/.test(normalized)) return 'ADDRESS_CS';
  if (address && /postal_code/.test(normalized)) return 'ADDRESS_POSTAL_CODE';
  if (address && /city/.test(normalized)) return 'ADDRESS_CITY';
  if (address && /cedex/.test(normalized)) return 'ADDRESS_CEDEX_NUMBER';
  if (address && /insee_code/.test(normalized)) return 'ADDRESS_INSEE_CODE';
  if (address && /country_code/.test(normalized)) return 'ADDRESS_COUNTRY_CODE';
  if (address && /\.country$/.test(normalized)) return 'ADDRESS_COUNTRY';
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
  const seenIdentifiers = new Set();
  const ignored = new Set([
    'confidence', 'source', 'bbox', 'page', 'evidence', 'normalization', 'verification',
    'requires_review', 'raw_value', 'source_text', 'source_path', 'cell_id',
  ]);

  function add(path, label, value, evidence = null) {
    if (!label || seen.has(path) || value == null || value === '') return;
    const identifier = /(?:^|_)(?:VAT_NUMBER|SIREN|SIRET)$/.test(label);
    const identifierKey = `${label}:${normalizedComparable(value)}`;
    if (identifier && seenIdentifiers.has(identifierKey)) return;
    seen.add(path);
    if (identifier) seenIdentifiers.add(identifierKey);
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

  function addLineItemFields(lines) {
    for (const field of lineItemTools.preannotate(lines, normalizedEvidence)) {
      add(field.path, field.label, field.extracted_value, field.evidence);
    }
  }

  function visit(value, path, inheritedRole = '') {
    if (value == null) return;
    if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, `${path}[${index}]`, inheritedRole));
      return;
    }
    if (typeof value !== 'object') {
      add(path, inferLabel(path, inheritedRole), value);
      return;
    }
    const roleName = String(value.role || value.party_role || '').toLowerCase();
    const contextualRole = {
      buyer: 'BUYER', supplier: 'SUPPLIER', ship_to: 'SHIP_TO', deliver_to: 'SHIP_TO',
      delivery_address: 'SHIP_TO', bill_to: 'BILL_TO', invoice_to: 'BILL_TO',
      payer: 'PAYER', ship_from: 'SHIP_FROM',
    }[roleName] || inheritedRole;
    if (Object.prototype.hasOwnProperty.call(value, 'value')) {
      add(path, inferLabel(path, contextualRole), value.value, normalizedEvidence(value));
      return;
    }
    for (const [key, child] of Object.entries(value)) {
      if (ignored.has(key)) continue;
      if (path === 'order' && key === 'line_items') {
        if (Array.isArray(child)) addLineItemFields(child);
        continue;
      }
      visit(child, path ? `${path}.${key}` : key, contextualRole);
    }
  }

  visit(extraction, '');
  return found;
}

function catalogName(label) {
  return state.catalog.find(item => item.label === label)?.name || label;
}

function catalogHelp(label) {
  return state.catalog.find(item => item.label === label)?.help
    || 'Encadrez uniquement la valeur imprimée correspondant à ce champ, sans les libellés ni les champs voisins.';
}

function catalogExample(label) {
  return state.catalog.find(item => item.label === label)?.example
    || 'Dans « Libellé : VALEUR », encadrez uniquement « VALEUR ».';
}

function catalogDefaultPath(label) {
  if (lineItemTools.isLineItemLabel(label)) return lineItemTools.canonicalPath(label, 0);
  return state.catalog.find(item => item.label === label)?.defaultPath
    || `manual.${String(label || 'field').toLowerCase()}`;
}

function fieldLineIndex(field) {
  return lineItemTools.indexFromPath(field?.path);
}

function fieldDisplayName(field) {
  const index = fieldLineIndex(field);
  return index >= 0
    ? `L${index + 1} · ${catalogName(field.label)}`
    : catalogName(field.label);
}

function contextualPath(template, previousPath = '') {
  let path = template;
  for (const collection of ['line_items', 'additional_charges', 'addresses', 'tax_identifiers']) {
    if (!path.includes(`${collection}[0]`)) continue;
    const match = String(previousPath).match(new RegExp(`${collection}\\[(\\d+)\\]`));
    if (match) path = path.replace(`${collection}[0]`, `${collection}[${match[1]}]`);
  }
  return path;
}

function availableCanonicalPath(label, previousPath = '', excludedIndex = -1) {
  const base = contextualPath(catalogDefaultPath(label), previousPath);
  const used = new Set(
    state.fields
      .filter((_, index) => index !== excludedIndex)
      .map(item => item.path),
  );
  if (!used.has(base)) return base;

  const collectionMatch = base.match(/(line_items|additional_charges|addresses|tax_identifiers)\[(\d+)\]/);
  if (collectionMatch) {
    for (let index = Number(collectionMatch[2]) + 1; index < 10000; index += 1) {
      const candidate = base.replace(
        `${collectionMatch[1]}[${collectionMatch[2]}]`,
        `${collectionMatch[1]}[${index}]`,
      );
      if (!used.has(candidate)) return candidate;
    }
  }

  for (let occurrence = 2; occurrence < 10000; occurrence += 1) {
    const candidate = base.endsWith('.value')
      ? `${base.slice(0, -6)}.alternatives[${occurrence - 1}].value`
      : `${base}.alternatives[${occurrence - 1}]`;
    if (!used.has(candidate)) return candidate;
  }
  return base;
}

function createLineGroupHeader(lineIndex, editable) {
  const fields = state.fields.filter(field => fieldLineIndex(field) === lineIndex);
  const reviewed = fields.filter(field => field.status !== 'UNREVIEWED').length;
  const header = document.createElement('section');
  header.className = 'line-item-group-header';
  header.dataset.lineIndex = String(lineIndex);

  const identity = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = `Ligne de commande ${lineIndex + 1}`;
  const path = document.createElement('code');
  path.textContent = `order.line_items[${lineIndex}]`;
  const progress = document.createElement('span');
  progress.className = 'line-item-progress';
  progress.textContent = `${reviewed}/${fields.length} champs vérifiés`;
  identity.append(title, path, progress);

  const actions = document.createElement('div');
  actions.className = 'line-item-group-actions';
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'button secondary compact';
  add.textContent = '+ Champ de ligne';
  add.disabled = !editable;
  add.addEventListener('click', event => {
    event.stopPropagation();
    addLineItemField(lineIndex);
  });
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'button danger compact';
  remove.textContent = 'Retirer la ligne';
  remove.disabled = !editable;
  remove.addEventListener('click', event => {
    event.stopPropagation();
    removeLineItem(lineIndex);
  });
  actions.append(add, remove);
  header.append(identity, actions);
  return header;
}

function addLineItemField(lineIndex) {
  const existing = new Set(
    state.fields
      .filter(field => fieldLineIndex(field) === lineIndex)
      .map(field => field.label),
  );
  const label = lineItemTools.FIELD_ORDER.find(candidate => !existing.has(candidate));
  if (!label) {
    return showMessage(
      $('annotationMessage'),
      `Tous les champs de la ligne ${lineIndex + 1} sont déjà présents.`,
      'error',
    );
  }
  const lastLineField = state.fields.reduce(
    (last, field, index) => fieldLineIndex(field) === lineIndex ? index : last,
    -1,
  );
  const insertionIndex = lastLineField + 1;
  state.fields.splice(insertionIndex, 0, lineItemTools.createField(label, lineIndex));
  state.selectedField = insertionIndex;
  markDirty();
  renderFields();
  fieldList.querySelector(`[data-index="${insertionIndex}"]`)?.scrollIntoView({
    behavior: 'smooth', block: 'center',
  });
}

function addLineItem() {
  const lineIndex = lineItemTools.nextIndex(state.fields);
  const fields = lineItemTools.createLine(lineIndex);
  const lastLineField = state.fields.reduce(
    (last, field, index) => fieldLineIndex(field) >= 0 ? index : last,
    -1,
  );
  const firstIndex = lastLineField >= 0 ? lastLineField + 1 : state.fields.length;
  state.fields.splice(firstIndex, 0, ...fields);
  state.selectedField = firstIndex;
  markDirty();
  renderFields();
  fieldList.querySelector(`[data-index="${firstIndex}"]`)?.scrollIntoView({
    behavior: 'smooth', block: 'center',
  });
  showMessage(
    $('annotationMessage'),
    `Ligne de commande ${lineIndex + 1} ajoutée avec l’index JSON line_items[${lineIndex}].`,
    'success',
  );
}

function compactLineItemIndexes() {
  state.fields = lineItemTools.compact(state.fields);
}

function removeLineItem(lineIndex) {
  const lineFields = state.fields.filter(field => fieldLineIndex(field) === lineIndex);
  if (!lineFields.length) return;
  if (state.fields.length === lineFields.length) {
    return showMessage(
      $('annotationMessage'),
      'Une annotation doit conserver au moins un champ.',
      'error',
    );
  }
  const meaningful = lineFields.some(field => (
    field.status !== 'UNREVIEWED' || field.corrected_value || field.comment
  ));
  if (meaningful && !window.confirm(
    `Retirer entièrement la ligne de commande ${lineIndex + 1} et ses ${lineFields.length} champs ?`,
  )) return;
  state.fields = state.fields.filter(field => fieldLineIndex(field) !== lineIndex);
  compactLineItemIndexes();
  state.selectedField = Math.min(state.selectedField, state.fields.length - 1);
  markDirty();
  renderFields();
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
  if (hasPermission('users.manage')) await Promise.all([
    loadUsers(),
    loadAuditEvents(),
    loadServiceTokenStatus(),
  ]);
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
  $('addLineItemButton').classList.toggle('hidden', !canAnnotate);
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
  const payload = await api('/documents?limit=1000');
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
    button.addEventListener('click', () => {
      if (
        state.dirty
        && state.activeDocument?.documentId !== item.documentId
        && !window.confirm('Des modifications ne sont pas enregistrées. Changer de document ?')
      ) return;
      openDocument(item.documentId);
    });
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
  state.fields = lineItemTools.compact(state.fields);
  state.selectedField = state.fields.length ? 0 : -1;
  state.zoom = 1;
  state.pageTextItems = [];
  state.dirty = false;
  $('reviewerIdentity').textContent = `${state.user.displayName || state.user.username} · ${state.user.username}`;
  $('annotationNote').value = document.latestAnnotation?.note || '';
  $('annotationNote').disabled = !(hasPermission('annotations.draft') || hasPermission('annotations.validate'));
  $('activeFilename').textContent = document.filename;
  $('activeHash').textContent = `SHA-256 ${document.pdfSha256}`;
  $('activeStatus').textContent = statusLabel(document.status);
  $('activeStatus').className = `badge ${document.status === 'DRAFT' ? 'draft' : ''}`;
  $('activeStatus').title = '';
  annotationEmpty.classList.add('hidden');
  annotationWorkspace.classList.remove('hidden');
  renderDocumentList();
  renderFields();
  await loadPdf(documentId);
}

function editableAnnotation() {
  return hasPermission('annotations.draft') || hasPermission('annotations.validate');
}

function updateJsonPreview() {
  const preview = $('annotationJsonPreview');
  if (!preview || !state.activeDocument) return;
  preview.textContent = JSON.stringify(annotationPayload('DRAFT'), null, 2);
}

function markDirty() {
  if (!state.activeDocument) return;
  state.dirty = true;
  $('activeStatus').classList.add('dirty');
  $('activeStatus').title = 'Modifications locales non enregistrées';
  updateJsonPreview();
}

function normalizedComparable(value) {
  return valueText(value).normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();
}

function inferDocumentTypeFromText(value) {
  const text = normalizedComparable(value)
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  if (/annulation.*commande/.test(text)) return 'order_cancellation';
  if (/confirmation.*commande|accuse.*commande/.test(text)) return 'order_confirmation';
  if (/demande.*(?:prix|offre)|request for quotation|\brfq\b/.test(text)) return 'request_for_quotation';
  if (/bon.*livraison|delivery note|packing list/.test(text)) return 'delivery_note';
  if (/avoir|credit note/.test(text)) return 'credit_note';
  if (/facture|invoice/.test(text)) return 'invoice';
  if (/devis|quotation|\boffre\b|\bquote\b/.test(text)) return 'quotation';
  if (/bon.*commande|purchase order|commande fournisseur/.test(text)) return 'purchase_order';
  if (/conditions generales|terms and conditions/.test(text)) return 'legal_terms';
  if (/contrat|contract|agreement/.test(text)) return 'contract';
  return '';
}

function normalizeSelectedValue(field, rawValue) {
  let value = String(rawValue || '').replace(/\s*\n\s*/g, ' ').replace(/\s+/g, ' ').trim();
  if (!value) return '';
  if (field.label === 'DOCUMENT_TYPE') return inferDocumentTypeFromText(value);
  if (field.label.endsWith('_SIREN') || field.label === 'SIREN') {
    const digits = value.replace(/\D/g, '');
    return digits.length === 9 ? digits : value;
  }
  if (field.label.endsWith('_SIRET') || field.label === 'SIRET') {
    const digits = value.replace(/\D/g, '');
    return digits.length === 14 ? digits : value;
  }
  if (field.label.endsWith('_VAT_NUMBER') || field.label === 'VAT_NUMBER') {
    const compact = value.replace(/[^A-Za-z0-9]/g, '').toUpperCase();
    return compact.length >= 10 ? compact : value;
  }
  return value;
}

function populateDocumentTypeSelect(select, other, currentValue) {
  select.replaceChildren();
  for (const [value, name] of DOCUMENT_TYPE_OPTIONS) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = name;
    select.appendChild(option);
  }
  const current = valueText(currentValue).trim();
  if (DOCUMENT_TYPE_VALUES.has(current)) {
    select.value = current;
    other.value = '';
    other.classList.add('hidden');
  } else {
    select.value = '__other__';
    other.value = current;
    other.classList.remove('hidden');
  }
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
  const editable = editableAnnotation();
  fieldList.replaceChildren();
  const renderedLineHeaders = new Set();
  state.fields.forEach((field, index) => {
    const lineIndex = fieldLineIndex(field);
    if (lineIndex >= 0 && !renderedLineHeaders.has(lineIndex)) {
      fieldList.appendChild(createLineGroupHeader(lineIndex, editable));
      renderedLineHeaders.add(lineIndex);
    }
    const node = $('fieldTemplate').content.firstElementChild.cloneNode(true);
    node.dataset.index = String(index);
    node.dataset.lineIndex = lineIndex >= 0 ? String(lineIndex) : '';
    node.classList.toggle('line-item-field', lineIndex >= 0);
    node.classList.toggle('selected', index === state.selectedField);
    node.classList.toggle('reviewed', ['CONFIRMED', 'CORRECTED'].includes(field.status));
    node.classList.toggle('problem', ['ABSENT', 'AMBIGUOUS'].includes(field.status));
    const label = node.querySelector('.field-label');
    const lineBadge = node.querySelector('.line-item-badge');
    lineBadge.classList.toggle('hidden', lineIndex < 0);
    lineBadge.textContent = lineIndex >= 0
      ? `Ligne ${lineIndex + 1} · index JSON ${lineIndex}`
      : '';
    populateLabelSelect(label, field.label);
    const helper = node.querySelector('.field-helper');
    const help = node.querySelector('.field-help');
    const example = node.querySelector('.field-example-value');
    help.textContent = catalogHelp(field.label);
    example.textContent = catalogExample(field.label);
    helper.open = index === state.selectedField;
    helper.addEventListener('click', event => event.stopPropagation());
    const path = node.querySelector('.field-path');
    const status = node.querySelector('.field-status');
    const extracted = node.querySelector('.extracted-value');
    const corrected = node.querySelector('.corrected-value');
    const documentType = node.querySelector('.document-type-select');
    const documentTypeOther = node.querySelector('.document-type-other');
    const remove = node.querySelector('.delete-field');
    const page = node.querySelector('.evidence-page');
    const bbox = node.querySelector('.evidence-bbox');
    const comment = node.querySelector('.field-comment');
    path.value = field.path;
    status.value = field.status;
    extracted.value = valueText(field.extracted_value);
    corrected.value = valueText(field.corrected_value);
    const isDocumentType = field.label === 'DOCUMENT_TYPE';
    corrected.classList.toggle('hidden', isDocumentType);
    documentType.classList.toggle('hidden', !isDocumentType);
    if (isDocumentType) populateDocumentTypeSelect(
      documentType,
      documentTypeOther,
      field.corrected_value ?? field.extracted_value,
    );
    else documentTypeOther.classList.add('hidden');
    page.value = field.evidence?.page || '';
    bbox.value = field.evidence?.bbox?.join(',') || '';
    comment.value = field.comment || '';
    label.disabled = !editable;
    path.disabled = !editable;
    status.disabled = !editable;
    corrected.disabled = !editable || field.status !== 'CORRECTED';
    documentType.disabled = !editable || field.status !== 'CORRECTED';
    documentTypeOther.disabled = !editable || field.status !== 'CORRECTED';
    remove.disabled = !editable;
    page.disabled = !editable;
    bbox.disabled = !editable;
    comment.disabled = !editable;
    node.addEventListener('click', () => selectField(index));
    label.addEventListener('change', event => {
      field.label = event.target.value;
      field.path = availableCanonicalPath(field.label, field.path, index);
      compactLineItemIndexes();
      markDirty();
      renderFields();
    });
    path.addEventListener('input', event => { field.path = event.target.value; markDirty(); });
    status.addEventListener('change', event => {
      field.status = event.target.value;
      if (isDocumentType && field.status === 'CORRECTED' && !field.corrected_value) {
        field.corrected_value = documentType.value === '__other__'
          ? documentTypeOther.value
          : documentType.value;
      }
      corrected.disabled = !editable || field.status !== 'CORRECTED';
      documentType.disabled = !editable || field.status !== 'CORRECTED';
      documentTypeOther.disabled = !editable || field.status !== 'CORRECTED';
      renderProgress();
      node.classList.toggle('reviewed', ['CONFIRMED', 'CORRECTED'].includes(field.status));
      node.classList.toggle('problem', ['ABSENT', 'AMBIGUOUS'].includes(field.status));
      markDirty();
    });
    corrected.addEventListener('input', event => { field.corrected_value = event.target.value; markDirty(); });
    documentType.addEventListener('change', event => {
      const custom = event.target.value === '__other__';
      documentTypeOther.classList.toggle('hidden', !custom);
      field.corrected_value = custom ? documentTypeOther.value : event.target.value;
      markDirty();
      if (custom) documentTypeOther.focus();
    });
    documentTypeOther.addEventListener('input', event => {
      field.corrected_value = event.target.value;
      markDirty();
    });
    page.addEventListener('input', () => updateEvidence(field, page.value, bbox.value));
    bbox.addEventListener('input', () => updateEvidence(field, page.value, bbox.value));
    comment.addEventListener('input', event => { field.comment = event.target.value; markDirty(); });
    remove.addEventListener('click', event => {
      event.stopPropagation();
      removeField(index);
    });
    fieldList.appendChild(node);
  });
  renderProgress();
  drawAnnotations();
  updateJsonPreview();
}

async function selectField(index) {
  if (index < 0 || index >= state.fields.length) return;
  state.selectedField = index;
  fieldList.querySelectorAll('.field-card').forEach((item, itemIndex) => {
    item.classList.toggle('selected', itemIndex === index);
    item.querySelector('.field-helper').open = itemIndex === index;
  });
  const targetPage = Number(state.fields[index].evidence?.page || 0);
  if (state.pdf && targetPage > 0 && targetPage <= state.pdf.numPages && targetPage !== state.page) {
    state.page = targetPage;
    await renderPdfPage();
  } else {
    drawAnnotations();
  }
}

function removeField(index) {
  if (state.fields.length <= 1) {
    return showMessage($('annotationMessage'), 'Une annotation doit conserver au moins un champ.', 'error');
  }
  const field = state.fields[index];
  const meaningful = field.status !== 'UNREVIEWED' || field.corrected_value || field.comment;
  if (meaningful && !window.confirm(`Retirer le champ « ${catalogName(field.label)} » ?`)) return;
  state.fields.splice(index, 1);
  compactLineItemIndexes();
  state.selectedField = Math.min(index, state.fields.length - 1);
  markDirty();
  renderFields();
}

function updateEvidence(field, pageValue, bboxValue) {
  const box = String(bboxValue || '').split(',').map(value => Number(value.trim()));
  if (box.length === 4 && box.every(Number.isFinite) && Number(pageValue) > 0) {
    field.evidence = { page: Number(pageValue), bbox: box };
  } else {
    field.evidence = null;
  }
  markDirty();
  drawAnnotations();
}

function renderProgress() {
  const reviewed = state.fields.filter(field => field.status !== 'UNREVIEWED').length;
  $('reviewProgress').textContent = `${reviewed}/${state.fields.length} champs vérifiés`;
  updateJsonPreview();
}

async function loadPdf(documentId) {
  if (state.pdfUrl) URL.revokeObjectURL(state.pdfUrl);
  const response = await api(`/documents/${encodeURIComponent(documentId)}/pdf`);
  const blob = await response.blob();
  state.pdfUrl = URL.createObjectURL(blob);
  state.pdf = await pdfjsLib.getDocument(state.pdfUrl).promise;
  state.page = 1;
  state.zoom = 1;
  state.pageTextItems = [];
  $('pageCount').textContent = String(state.pdf.numPages);
  await renderPdfPage();
}

async function renderPdfPage() {
  if (!state.pdf) return;
  if (state.rendering) {
    state.pendingRender = true;
    return;
  }
  state.rendering = true;
  try {
    const page = await state.pdf.getPage(state.page);
    const base = page.getViewport({ scale: 1 });
    const available = Math.max(360, $('pdfCanvasWrap').clientWidth - 40);
    const fitScale = Math.min(1.7, Math.max(.7, available / base.width));
    const scale = fitScale * state.zoom;
    const viewport = page.getViewport({ scale });
    const context = pdfCanvas.getContext('2d');
    const width = Math.max(1, Math.round(viewport.width));
    const height = Math.max(1, Math.round(viewport.height));
    pdfCanvas.width = width;
    pdfCanvas.height = height;
    annotationCanvas.width = width;
    annotationCanvas.height = height;
    canvasStage.style.width = `${width}px`;
    canvasStage.style.height = `${height}px`;
    await page.render({ canvasContext: context, viewport }).promise;
    const textContent = await page.getTextContent();
    state.pageTextItems = textContent.items
      .filter(item => String(item.str || '').trim())
      .map(item => {
        const transform = pdfjsLib.Util.transform(viewport.transform, item.transform);
        const itemHeight = Math.max(4, Math.hypot(transform[2], transform[3]));
        const itemWidth = Math.max(2, Number(item.width || 0) * scale);
        return {
          text: String(item.str).trim(),
          x0: transform[4],
          y0: transform[5] - itemHeight,
          x1: transform[4] + itemWidth,
          y1: transform[5] + itemHeight * .2,
        };
      });
    $('currentPage').textContent = String(state.page);
    $('zoomLevel').textContent = `${Math.round(state.zoom * 100)} %`;
    drawAnnotations();
  } finally {
    state.rendering = false;
    if (state.pendingRender) {
      state.pendingRender = false;
      window.setTimeout(() => renderPdfPage(), 0);
    }
  }
}

async function setZoom(value) {
  state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(value * 100) / 100));
  await renderPdfPage();
}

async function focusSelectedField() {
  const field = state.fields[state.selectedField];
  if (!field?.evidence?.bbox) {
    return showMessage($('annotationMessage'), 'Ce champ ne possède pas encore de zone PDF.', 'error');
  }
  const page = Number(field.evidence.page || 0);
  if (page && page !== state.page) state.page = page;
  if (state.zoom < 2) state.zoom = 2;
  await renderPdfPage();
  const [x0, y0, x1, y1] = field.evidence.bbox;
  const wrap = $('pdfCanvasWrap');
  const centerX = canvasStage.offsetLeft + (x0 + x1) / 2000 * annotationCanvas.width;
  const centerY = canvasStage.offsetTop + (y0 + y1) / 2000 * annotationCanvas.height;
  wrap.scrollTo({
    left: Math.max(0, centerX - wrap.clientWidth / 2),
    top: Math.max(0, centerY - wrap.clientHeight / 2),
    behavior: 'smooth',
  });
}

function drawAnnotations(preview = null) {
  const context = annotationCanvas.getContext('2d');
  context.clearRect(0, 0, annotationCanvas.width, annotationCanvas.height);
  state.fields.forEach((field, index) => {
    if (!state.showOtherAnnotations && index !== state.selectedField) return;
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
    if (index === state.selectedField) {
      context.fillStyle = '#2356d8';
      for (const [handleX, handleY] of [[x, y], [x + width, y], [x, y + height], [x + width, y + height]]) {
        context.beginPath();
        context.arc(handleX, handleY, 4, 0, Math.PI * 2);
        context.fill();
      }
    }
    context.fillStyle = index === state.selectedField ? '#2356d8' : '#0b8c65';
    context.font = '11px sans-serif';
    context.fillText(fieldDisplayName(field), x + 3, Math.max(11, y - 3));
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

function canvasPoint(event) {
  const rect = annotationCanvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left) * annotationCanvas.width / rect.width,
    y: (event.clientY - rect.top) * annotationCanvas.height / rect.height,
  };
}

function selectedPageText(box) {
  const [x0, y0, x1, y1] = box;
  const selected = state.pageTextItems.filter(item => {
    const overlapX = Math.max(0, Math.min(x1, item.x1) - Math.max(x0, item.x0));
    const overlapY = Math.max(0, Math.min(y1, item.y1) - Math.max(y0, item.y0));
    const area = Math.max(1, (item.x1 - item.x0) * (item.y1 - item.y0));
    return overlapX * overlapY / area >= .2
      || ((item.x0 + item.x1) / 2 >= x0 && (item.x0 + item.x1) / 2 <= x1
        && (item.y0 + item.y1) / 2 >= y0 && (item.y0 + item.y1) / 2 <= y1);
  }).sort((left, right) => left.y0 - right.y0 || left.x0 - right.x0);
  const lines = [];
  for (const item of selected) {
    const center = (item.y0 + item.y1) / 2;
    let line = lines.find(candidate => Math.abs(candidate.center - center) <= Math.max(4, (item.y1 - item.y0) * .65));
    if (!line) {
      line = { center, items: [] };
      lines.push(line);
    }
    line.items.push(item);
  }
  return lines
    .sort((left, right) => left.center - right.center)
    .map(line => line.items.sort((left, right) => left.x0 - right.x0).map(item => item.text).join(' '))
    .join('\n').trim();
}

async function updateFieldFromRegion(fieldIndex, canvasBox, normalizedBox) {
  const field = state.fields[fieldIndex];
  const documentId = state.activeDocument?.documentId;
  const requestId = ++state.regionRequest;
  if (!field || !documentId) return;
  const localText = selectedPageText(canvasBox);
  let text = localText;
  let source = localText ? 'pdf.js' : 'none';
  showMessage($('annotationMessage'), 'Lecture de la zone en cours…');
  try {
    const result = await api(`/documents/${encodeURIComponent(documentId)}/region-text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: state.page, bbox: normalizedBox }),
    });
    if (state.activeDocument?.documentId !== documentId || state.regionRequest !== requestId) return;
    if (result.text) {
      text = result.text;
      source = result.source || 'zone PDF';
    }
  } catch (error) {
    if (!localText) {
      showMessage($('annotationMessage'), `Zone mémorisée, mais sa lecture a échoué : ${error.message}`, 'error');
      return;
    }
  }
  if (state.fields[fieldIndex] !== field || state.regionRequest !== requestId) return;
  const value = normalizeSelectedValue(field, text);
  if (!value) {
    showMessage(
      $('annotationMessage'),
      field.label === 'DOCUMENT_TYPE'
        ? 'Zone mémorisée. Choisissez le type dans la liste corrigée.'
        : 'Zone mémorisée, mais aucun texte lisible n’y a été trouvé.',
      'error',
    );
    renderFields();
    return;
  }
  if (normalizedComparable(value) === normalizedComparable(field.extracted_value)) {
    field.status = 'CONFIRMED';
    field.corrected_value = null;
  } else {
    field.status = 'CORRECTED';
    field.corrected_value = value;
  }
  markDirty();
  renderFields();
  showMessage($('annotationMessage'), `Valeur relue depuis la zone (${source}) et JSON mis à jour.`, 'success');
}

annotationCanvas.addEventListener('pointerdown', event => {
  if (state.selectedField < 0 || !editableAnnotation()) return;
  drawStart = canvasPoint(event);
  annotationCanvas.setPointerCapture(event.pointerId);
});
annotationCanvas.addEventListener('pointermove', event => {
  if (!drawStart) return;
  const { x, y } = canvasPoint(event);
  drawAnnotations({
    x: Math.min(drawStart.x, x), y: Math.min(drawStart.y, y),
    width: Math.abs(x - drawStart.x), height: Math.abs(y - drawStart.y),
  });
});
annotationCanvas.addEventListener('pointerup', async event => {
  if (!drawStart || state.selectedField < 0) return;
  const end = canvasPoint(event);
  const x0 = Math.min(drawStart.x, end.x);
  const y0 = Math.min(drawStart.y, end.y);
  const x1 = Math.max(drawStart.x, end.x);
  const y1 = Math.max(drawStart.y, end.y);
  drawStart = null;
  if (x1 - x0 < 5 || y1 - y0 < 5) return drawAnnotations();
  const fieldIndex = state.selectedField;
  const normalizedBox = [x0, y0, x1, y1].map((value, index) => Math.round(
    value / (index % 2 === 0 ? annotationCanvas.width : annotationCanvas.height) * 1000,
  ));
  state.fields[fieldIndex].evidence = {
    page: state.page,
    bbox: normalizedBox,
  };
  markDirty();
  renderFields();
  await updateFieldFromRegion(fieldIndex, [x0, y0, x1, y1], normalizedBox);
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
  const lineErrors = lineItemTools.validate(state.fields);
  if (lineErrors.length) {
    return showMessage(message, lineErrors.join(' '), 'error');
  }
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

async function loadServiceTokenStatus() {
  const status = await api('/admin/service-token');
  const label = $('serviceTokenStatus');
  const button = $('rotateServiceTokenButton');
  if (status.source === 'environment') {
    label.textContent = status.rotatable
      ? 'Un jeton issu de la configuration est actif. Vous pouvez le remplacer ici par un jeton géré par JIN Studio.'
      : 'Un jeton issu de la configuration est actif, mais le stockage partagé des jetons Studio n’est pas configuré.';
  } else if (status.configured) {
    label.textContent = 'Jeton partagé actif, stocké dans le volume sécurisé de JIN Studio.';
  } else {
    label.textContent = 'Aucun jeton machine configuré.';
  }
  if (!status.mcpShared) {
    label.textContent += ' MCP utilise un jeton distinct géré par la configuration de la VM.';
  }
  button.disabled = !status.rotatable;
  button.title = status.rotatable ? '' : 'Le stockage partagé des jetons JIN Studio n’est pas configuré';
}

async function rotateServiceTokenFromUi() {
  const accepted = window.confirm(
    'Créer un nouveau jeton révoquera immédiatement le précédent. Continuer ?',
  );
  if (!accepted) return;
  try {
    const payload = await api('/admin/service-token/rotate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    $('serviceTokenValue').value = payload.token;
    $('serviceTokenValue').type = 'password';
    $('toggleServiceTokenButton').textContent = 'Afficher';
    renderConnectionConfigs();
    showMessage(
      $('serviceTokenMessage'),
      'Jeton créé. Copiez-le maintenant : il ne sera plus affiché après effacement ou rechargement.',
      'success',
    );
    await Promise.all([loadServiceTokenStatus(), loadAuditEvents()]);
  } catch (error) {
    showMessage($('serviceTokenMessage'), error.message, 'error');
  }
}

async function copyServiceToken() {
  const value = $('serviceTokenValue').value;
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    showMessage($('serviceTokenMessage'), 'Jeton copié dans le presse-papiers.', 'success');
  } catch (_) {
    $('serviceTokenValue').select();
    showMessage($('serviceTokenMessage'), 'Copie automatique indisponible : utilisez Ctrl+C.', 'error');
  }
}

function hideServiceToken() {
  $('serviceTokenValue').value = '';
  $('serviceTokenValue').type = 'password';
  $('toggleServiceTokenButton').textContent = 'Afficher';
  renderConnectionConfigs();
  showMessage($('serviceTokenMessage'), 'Jeton effacé de cette page. Le jeton actif côté serveur n’a pas été révoqué.', 'success');
}

function toggleServiceTokenVisibility() {
  const input = $('serviceTokenValue');
  const reveal = input.type === 'password';
  input.type = reveal ? 'text' : 'password';
  $('toggleServiceTokenButton').textContent = reveal ? 'Masquer' : 'Afficher';
}

function normalizedConnectionBaseUrl() {
  let value = $('connectionBaseUrl').value.trim() || window.location.origin;
  if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
  return value.replace(/\/+$/, '');
}

function connectionTokenOrPlaceholder() {
  return $('serviceTokenValue').value.trim() || '<JETON_JIN_STUDIO>';
}

function renderConnectionConfigs() {
  const baseUrl = normalizedConnectionBaseUrl();
  const token = connectionTokenOrPlaceholder();
  const authorization = `Bearer ${token}`;
  const mcpUrl = `${baseUrl}/mcp`;
  const usesGenericJson = $('mcpConfigFormat').value === 'json';

  if (usesGenericJson) {
    $('mcpConnectionConfig').value = JSON.stringify({
      mcpServers: {
        'jin-studio': {
          type: 'streamable-http',
          url: mcpUrl,
          headers: { Authorization: authorization },
        },
      },
    }, null, 2);
  } else {
    $('mcpConnectionConfig').value = [
      '[mcp_servers.jin_studio]',
      `url = ${JSON.stringify(mcpUrl)}`,
      `http_headers = { Authorization = ${JSON.stringify(authorization)} }`,
      'required = true',
    ].join('\n');
  }
  $('mcpConfigLocation').textContent = usesGenericJson
    ? 'À coller dans la section MCP du client compatible Streamable HTTP.'
    : 'Pour Codex : à coller dans ~/.codex/config.toml.';

  $('apiConnectionConfig').value = JSON.stringify({
    baseUrl: `${baseUrl}/api/studio`,
    headers: { Authorization: authorization },
  }, null, 2);
}

async function copyGeneratedConfig(elementId, description) {
  const element = $(elementId);
  try {
    await navigator.clipboard.writeText(element.value);
    showMessage($('serviceTokenMessage'), `${description} copiée dans le presse-papiers.`, 'success');
  } catch (_) {
    element.focus();
    element.select();
    showMessage($('serviceTokenMessage'), 'Copie automatique indisponible : utilisez Ctrl+C.', 'error');
  }
}

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
$('rotateServiceTokenButton').addEventListener('click', rotateServiceTokenFromUi);
$('copyServiceTokenButton').addEventListener('click', copyServiceToken);
$('toggleServiceTokenButton').addEventListener('click', toggleServiceTokenVisibility);
$('hideServiceTokenButton').addEventListener('click', hideServiceToken);
$('connectionBaseUrl').addEventListener('input', renderConnectionConfigs);
$('serviceTokenValue').addEventListener('input', renderConnectionConfigs);
$('mcpConfigFormat').addEventListener('change', renderConnectionConfigs);
$('copyMcpConfigButton').addEventListener('click', () => copyGeneratedConfig('mcpConnectionConfig', 'Configuration MCP'));
$('copyApiConfigButton').addEventListener('click', () => copyGeneratedConfig('apiConnectionConfig', 'Configuration API'));
$('documentInput').addEventListener('change', event => uploadDocument(event.target.files?.[0]));
$('documentSearch').addEventListener('input', renderDocumentList);
$('documentStatusFilter').addEventListener('change', renderDocumentList);
$('confirmAllButton').addEventListener('click', () => {
  state.fields.forEach(field => { if (field.status === 'UNREVIEWED') field.status = 'CONFIRMED'; });
  markDirty();
  renderFields();
});
$('addFieldButton').addEventListener('click', () => {
  const index = state.fields.length;
  const label = 'ORDER_NUMBER';
  state.fields.push({ path: availableCanonicalPath(label), label, status: 'UNREVIEWED', extracted_value: '', corrected_value: null, evidence: null, comment: '' });
  state.selectedField = index;
  markDirty();
  renderFields();
  fieldList.lastElementChild?.scrollIntoView({ behavior: 'smooth', block: 'center' });
});
$('addLineItemButton').addEventListener('click', addLineItem);
$('saveDraftButton').addEventListener('click', () => saveAnnotation('DRAFT'));
$('validateButton').addEventListener('click', () => saveAnnotation('HUMAN_VALIDATED'));
$('createDatasetButton').addEventListener('click', createDataset);
$('previousPageButton').addEventListener('click', async () => { if (state.page > 1) { state.page -= 1; await renderPdfPage(); } });
$('nextPageButton').addEventListener('click', async () => { if (state.pdf && state.page < state.pdf.numPages) { state.page += 1; await renderPdfPage(); } });
$('zoomOutButton').addEventListener('click', () => setZoom(state.zoom - ZOOM_STEP));
$('zoomInButton').addEventListener('click', () => setZoom(state.zoom + ZOOM_STEP));
$('fitWidthButton').addEventListener('click', () => setZoom(1));
$('focusFieldButton').addEventListener('click', focusSelectedField);
showOtherAnnotationsToggle.addEventListener('change', event => {
  state.showOtherAnnotations = event.target.checked;
  try {
    window.localStorage.setItem(
      OTHER_ANNOTATIONS_VISIBILITY_KEY,
      String(state.showOtherAnnotations),
    );
  } catch (_) { /* The visual preference remains active for this page. */ }
  drawAnnotations();
});
$('annotationNote').addEventListener('input', markDirty);

document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && state.activeDocument) {
    event.preventDefault();
    saveAnnotation('DRAFT');
  }
});

window.addEventListener('beforeunload', event => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = '';
});

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
$('connectionBaseUrl').value = window.location.origin;
renderConnectionConfigs();
restoreSession();
