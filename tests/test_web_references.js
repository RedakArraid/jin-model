'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('const lineReferenceLabel =');
const end = source.indexOf('\n};', start) + 3;
assert.ok(start >= 0 && end > start, 'line reference formatter must be present');
const context = vm.createContext({});
vm.runInContext(source.slice(start, end) + '\nthis.label = lineReferenceLabel;', context);

test('line references hide repeated source prefixes but show quotes and derogations', () => {
  const label = context.label({
    material_number: 'EL7716780266',
    supplier_material_number: '7716780266',
    material_reference_source_prefix: 'EL',
    quote_numbers: ['DV-100', 'DV-200'],
    quote_number: 'DV-100',
    derogation_numbers: ['DER-77'],
  });

  assert.equal(
    label,
    '7716780266 · Devis DV-100, DV-200 · Dérog. DER-77',
  );
  assert.doesNotMatch(label, /Préfixe source/);
});

test('line references remain compact when no commercial metadata exists', () => {
  assert.equal(context.label({ material_number: 'ABC-123' }), 'ABC-123');
});
