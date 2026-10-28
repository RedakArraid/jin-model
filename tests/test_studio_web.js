'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/studio.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '../web/studio.js'), 'utf8');

test('JIN Studio contains every static DOM id used by its frontend', () => {
  const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]));
  const used = [...js.matchAll(/\$\(['"]([^'"]+)['"]\)/g)].map(match => match[1]);
  const missing = [...new Set(used.filter(id => !ids.has(id)))];
  assert.deepEqual(missing, []);
});

test('JIN Studio keeps document opening busy until completion, including errors', async () => {
  assert.match(html, /id="documentOpenStatus"[^>]*aria-live="polite"/);
  assert.match(html, /id="documentOpenOverlay"/);
  const openDocumentSource = js.match(/async function openDocument\(documentId\) \{[\s\S]*?\n\}/)?.[0];
  assert.ok(openDocumentSource);

  for (const shouldFail of [false, true]) {
    const events = [];
    let settle;
    const state = { openingDocumentId: null };
    const context = {
      state,
      startDocumentOpening(documentId) {
        state.openingDocumentId = documentId;
        events.push('start');
      },
      openDocumentContents() {
        events.push('request');
        return new Promise((resolve, reject) => { settle = shouldFail ? reject : resolve; });
      },
      finishDocumentOpening() {
        state.openingDocumentId = null;
        events.push('finish');
      },
    };
    vm.runInNewContext(`${openDocumentSource}\nglobalThis.openDocument = openDocument;`, context);
    const opening = context.openDocument('document-1');
    assert.deepEqual(events, ['start', 'request']);
    assert.equal(state.openingDocumentId, 'document-1');
    settle(shouldFail ? new Error('PDF indisponible') : undefined);
    if (shouldFail) await assert.rejects(opening, /PDF indisponible/);
    else await opening;
    assert.deepEqual(events, ['start', 'request', 'finish']);
    assert.equal(state.openingDocumentId, null);
  }
});

test('PDF rendering queued behind an earlier page remains awaitable', async () => {
  const renderSource = js.match(/async function renderPdfPage\(\) \{[\s\S]*?\n\}/)?.[0];
  assert.ok(renderSource);
  let finishFirstRender;
  let renderCount = 0;
  const page = {
    getViewport() { return { width: 100, height: 100 }; },
    render() {
      renderCount += 1;
      return {
        promise: renderCount === 1
          ? new Promise(resolve => { finishFirstRender = resolve; })
          : Promise.resolve(),
      };
    },
    getTextContent() { return Promise.resolve({ items: [] }); },
  };
  const state = {
    pdf: { getPage: async () => page },
    page: 1,
    zoom: 1,
    rendering: false,
    pendingRender: false,
    pendingRenderWaiters: [],
  };
  const context = {
    state,
    pdfCanvas: { getContext() { return {}; } },
    annotationCanvas: {},
    canvasStage: { style: {} },
    $(id) { return id === 'pdfCanvasWrap' ? { clientWidth: 500 } : {}; },
    drawAnnotations() {},
    window: { setTimeout },
  };
  vm.runInNewContext(`${renderSource}\nglobalThis.renderPdfPage = renderPdfPage;`, context);
  const first = context.renderPdfPage();
  const queued = context.renderPdfPage();
  let queuedComplete = false;
  queued.then(() => { queuedComplete = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(renderCount, 1);
  assert.equal(queuedComplete, false);
  finishFirstRender();
  await Promise.all([first, queued]);
  assert.equal(renderCount, 2);
  assert.equal(queuedComplete, true);
});

test('JIN Studio uses same-origin sessions and CSRF without browser bearer tokens', () => {
  assert.match(js, /credentials: 'same-origin'/);
  assert.match(js, /headers\['X-CSRF-Token'\]/);
  assert.doesNotMatch(js, /jinStudioToken/);
  assert.doesNotMatch(js, /Authorization: `Bearer/);
  assert.doesNotMatch(js, /[?&](?:token|access_token)=/);
});

test('JIN Studio exposes role-based user administration', () => {
  assert.match(html, /id="usersSection"/);
  assert.match(html, /id="createUserForm"/);
  assert.match(js, /users\.manage/);
  assert.match(js, /audit-events/);
});

test('JIN Studio exposes spatial annotation and explicit human validation', () => {
  assert.match(html, /id="annotationCanvas"/);
  assert.match(html, /Valider pour l’apprentissage/);
  assert.match(js, /HUMAN_VALIDATED/);
  assert.match(js, /bbox:/);
});

test('JIN Studio can resolve displayed field values and still attempts PDF rendering', () => {
  assert.match(js, /function effectiveValue\(field\)/);
  assert.match(js, /field\.status === 'CORRECTED' \? field\.corrected_value : field\.extracted_value/);
  assert.match(js, /fieldRenderError/);
  assert.match(js, /await loadPdf\(documentId\)/);
});

test('JIN Studio asks the server for geometric frames on model reread', () => {
  assert.match(js, /\/preannotation`/);
  assert.match(js, /firstFramedFieldIndex/);
  assert.match(js, /proposal\.framedFieldCount/);
  assert.match(html, /studio\.js\?v=28/);
});

test('JIN Studio launches and follows isolated training runs', () => {
  assert.match(html, /worker isolé/);
  assert.match(js, /Lancer l’entraînement/);
  assert.match(js, /currentStep/);
  assert.match(js, /setInterval/);
});
