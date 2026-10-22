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

test('JIN Studio keeps credentials out of URLs and sends an authorization header', () => {
  assert.match(js, /sessionStorage\.setItem\('jinStudioToken'/);
  assert.match(js, /Authorization: `Bearer \$\{state\.token\}`/);
  assert.doesNotMatch(js, /[?&](?:token|access_token)=/);
});

test('JIN Studio exposes spatial annotation and explicit human validation', () => {
  assert.match(html, /id="annotationCanvas"/);
  assert.match(html, /Valider pour l’apprentissage/);
  assert.match(js, /HUMAN_VALIDATED/);
  assert.match(js, /bbox:/);
});

test('JIN Studio launches and follows isolated training runs', () => {
  assert.match(html, /worker isolé/);
  assert.match(js, /Lancer l’entraînement/);
  assert.match(js, /currentStep/);
  assert.match(js, /setInterval/);
});
