'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../web/studio.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '../web/studio.js'), 'utf8');

test('JIN Studio contains every static DOM id used by its frontend', () => {
  const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]));
  const used = [...js.matchAll(/\$\(['"]([^'"]+)['"]\)/g)].map(match => match[1]);
  const missing = [...new Set(used.filter(id => !ids.has(id)))];
  assert.deepEqual(missing, []);
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
  assert.match(html, /studio\.js\?v=27/);
});

test('JIN Studio launches and follows isolated training runs', () => {
  assert.match(html, /worker isolé/);
  assert.match(js, /Lancer l’entraînement/);
  assert.match(js, /currentStep/);
  assert.match(js, /setInterval/);
});
