'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

test('every static DOM id used by the frontend exists', () => {
  const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]));
  const dynamicIds = new Set(['pdfPages', 'bboxOcrNote']);
  const used = [...js.matchAll(/getElementById\(['"]([^'"]+)['"]\)/g)]
    .map(match => match[1]);
  const missing = [...new Set(used.filter(id => !ids.has(id) && !dynamicIds.has(id)))];
  assert.deepEqual(missing, []);
});

test('commercial metadata, tax provenance and both JSON exports are visible', () => {
  for (const id of [
    'secTaxIdentifiers', 'taxIdentifiers', 'secReferences', 'referencesTable',
    'secLines', 'secCharges', 'downloadBtn', 'downloadRawBtn',
  ]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /Valeur imprimée ou calculée à partir d’un SIREN\/SIRET valide/);
  assert.match(html, /Les ports, taxes et contributions sont exclus de cette liste/);
  assert.match(html, /styles\.css\?v=5\.12\.0/);
  assert.match(html, /app\.js\?v=5\.12\.0/);
});
