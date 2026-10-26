'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const lines = require('../web/studio_line_items.js');

test('new order lines receive explicit monotonically increasing JSON indexes', () => {
  const first = lines.createLine(0);
  const second = lines.createLine(lines.nextIndex(first));
  const fields = [...first, ...second];

  assert.equal(first.length, 6);
  assert.equal(second.length, 6);
  assert.ok(first.every(field => field.path.includes('order.line_items[0].')));
  assert.ok(second.every(field => field.path.includes('order.line_items[1].')));
  assert.deepEqual(lines.indices(fields), [0, 1]);
  assert.deepEqual(lines.validate(fields), []);
});

test('removing a whole line compacts following indexes without changing values', () => {
  const first = lines.createLine(0);
  const third = lines.createLine(2).map((field, index) => ({
    ...field,
    extracted_value: `value-${index}`,
  }));

  const compacted = lines.compact([...first, ...third]);
  const formerThird = compacted.slice(first.length);

  assert.ok(formerThird.every(field => field.path.includes('order.line_items[1].')));
  assert.deepEqual(
    formerThird.map(field => field.extracted_value),
    third.map(field => field.extracted_value),
  );
  assert.deepEqual(lines.validate(compacted), []);
});

test('regular fields always stay above the complete order-line block', () => {
  const firstLine = lines.createLine(0);
  const secondLine = lines.createLine(1);
  const orderNumber = {
    path: 'order.customer_order_number.value',
    label: 'ORDER_NUMBER',
    extracted_value: 'PO-42',
  };
  const deliveryDate = {
    path: 'order.requested_delivery_date.value',
    label: 'DELIVERY_DATE',
    extracted_value: '2026-10-26',
  };

  const ordered = lines.compact([
    secondLine[0],
    orderNumber,
    ...firstLine,
    ...secondLine.slice(1),
    deliveryDate,
  ]);

  assert.deepEqual(ordered.slice(0, 2), [orderNumber, deliveryDate]);
  assert.ok(ordered.slice(2, 8).every(field => lines.indexFromPath(field.path) === 0));
  assert.ok(ordered.slice(8).every(field => lines.indexFromPath(field.path) === 1));
  assert.equal(ordered[2], firstLine[0]);
  assert.equal(ordered[8], secondLine[0]);
});

test('line validation reports gaps and line fields without an array index', () => {
  const gap = lines.createLine(1);
  assert.match(lines.validate(gap).join(' '), /line_items\[0\].*absent/);

  const unindexed = [{
    ...lines.createField('LINE_ITEM_QUANTITY', 0),
    path: 'manual.quantity',
  }];
  assert.match(lines.validate(unindexed).join(' '), /order\.line_items\[index\]/);
});

test('preannotation keeps one semantic field per source line', () => {
  const fields = lines.preannotate([{
    line_number: '10',
    references: {
      article_number: '7736507169',
      material_number: '7736507169',
    },
    description: 'CHAUFFE BAINS',
    quantity: 2,
    unit: 'PI',
    pricing: { unit_price: 700, net_unit_price: 683.4 },
    amounts: { net: 1366.8, total: 1366.8 },
  }]);

  assert.equal(fields.filter(field => field.label === 'LINE_ITEM_REFERENCE').length, 1);
  assert.equal(fields.filter(field => field.label === 'LINE_ITEM_UNIT_PRICE').length, 1);
  assert.equal(fields.filter(field => field.label === 'LINE_ITEM_TOTAL').length, 1);
  assert.equal(
    fields.find(field => field.label === 'LINE_ITEM_UNIT_PRICE').extracted_value,
    683.4,
  );
  assert.ok(fields.every(field => field.path.includes('order.line_items[0].')));
});
