'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('const fmt =');
const end = source.indexOf('\n};', source.indexOf('const unitPriceLabel =', start)) + 3;
assert.ok(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end) + '\nthis.price = unitPriceLabel; this.money = money;', context);
const normalizedStart = source.indexOf('function normalizedLine');
const normalizedEnd = source.indexOf('\n}\n', normalizedStart) + 3;
assert.ok(normalizedStart >= 0 && normalizedEnd > normalizedStart);
vm.runInContext(source.slice(normalizedStart, normalizedEnd) + '\nthis.normalizedLine = normalizedLine;', context);

test('unit prices keep printed decimals while document totals use cents', () => {
  assert.equal(context.price({ unit_price: 7.945 }, 'EUR'), '7,945 EUR');
  assert.equal(context.price({ unit_price: 30, net_unit_price: 15.075 }, 'EUR'), '15,075 EUR');
  assert.equal(context.money(15.075, 'EUR'), '15,08 EUR');
});

test('price bases, zero prices and missing values are not silently changed', () => {
  assert.equal(context.price({ unit_price: 200, price_unit: 100, uom: 'PCE' }, 'EUR'), '200,00 EUR / 100 PCE');
  assert.equal(context.price({ unit_price: 30, net_unit_price: 0 }, 'EUR'), '0,00 EUR');
  assert.equal(context.price({}, 'EUR'), null);
});

test('delivery weeks are displayed for raw and clean line contracts', () => {
  assert.equal(context.normalizedLine({ delivery_week: '44/26' }).requested_delivery_date, '44/26');
  assert.equal(context.normalizedLine({
    references: { material_number: '7716780595' },
    delivery: { week: '45/26' },
  }).requested_delivery_date, '45/26');
});
