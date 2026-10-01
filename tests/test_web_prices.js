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
