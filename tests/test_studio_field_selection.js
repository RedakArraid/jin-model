'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const selection = require('../web/studio_field_selection.js');

function field(name, scope = 'HEADER') {
  return { name, scope };
}

const scopeOf = item => item.scope;

test('an adjacent multi-selection moves as a stable visual block', () => {
  const fields = [field('a'), field('b'), field('c'), field('d')];
  const result = selection.move(fields, [fields[1], fields[2]], -1, scopeOf);

  assert.equal(result.moved, true);
  assert.deepEqual(result.fields.map(item => item.name), ['b', 'c', 'a', 'd']);
  assert.deepEqual(fields.map(item => item.name), ['a', 'b', 'c', 'd']);
});

test('a multi-selection moves down without reversing its fields', () => {
  const fields = [field('a'), field('b'), field('c'), field('d')];
  const result = selection.move(fields, [fields[1], fields[2]], 1, scopeOf);

  assert.equal(result.moved, true);
  assert.deepEqual(result.fields.map(item => item.name), ['a', 'd', 'b', 'c']);
});

test('non-adjacent fields form a block at the highest field and move up together', () => {
  const fields = [field('a'), field('b'), field('c'), field('d'), field('e')];
  const selected = [fields[1], fields[3]];
  const firstMove = selection.move(fields, selected, -1, scopeOf);
  const blockedMove = selection.move(firstMove.fields, selected, -1, scopeOf);

  assert.deepEqual(firstMove.fields.map(item => item.name), ['b', 'd', 'a', 'c', 'e']);
  assert.equal(blockedMove.moved, false);
  assert.deepEqual(blockedMove.fields.map(item => item.name), ['b', 'd', 'a', 'c', 'e']);
});

test('non-adjacent fields form a block at the lowest field and move down together', () => {
  const fields = [field('a'), field('b'), field('c'), field('d'), field('e')];
  const selected = [fields[1], fields[3]];
  const firstMove = selection.move(fields, selected, 1, scopeOf);
  const blockedMove = selection.move(firstMove.fields, selected, 1, scopeOf);

  assert.deepEqual(firstMove.fields.map(item => item.name), ['a', 'c', 'e', 'b', 'd']);
  assert.equal(blockedMove.moved, false);
  assert.deepEqual(blockedMove.fields.map(item => item.name), ['a', 'c', 'e', 'b', 'd']);
});

test('selected fields never cross annotation compartments', () => {
  const headerA = field('header-a', 'HEADER');
  const footerA = field('footer-a', 'FOOTER');
  const headerB = field('header-b', 'HEADER');
  const footerB = field('footer-b', 'FOOTER');
  const fields = [headerA, footerA, headerB, footerB];

  const result = selection.move(fields, [headerB, footerB], -1, scopeOf);

  assert.equal(result.moved, true);
  assert.deepEqual(
    result.fields.map(item => item.name),
    ['header-b', 'footer-b', 'header-a', 'footer-a'],
  );
});

test('each annotation compartment packs and moves its selected block independently', () => {
  const headerA = field('header-a', 'HEADER');
  const footerA = field('footer-a', 'FOOTER');
  const headerB = field('header-b', 'HEADER');
  const footerB = field('footer-b', 'FOOTER');
  const fields = [headerA, footerA, headerB, footerB];

  const result = selection.move(fields, [headerA, footerB], -1, scopeOf);

  assert.equal(result.moved, true);
  assert.deepEqual(result.fields, [headerA, footerB, headerB, footerA]);
});

test('order-line fields stay inside their own line', () => {
  const line0a = field('line-0-a', 'LINE_ITEMS:0');
  const line1a = field('line-1-a', 'LINE_ITEMS:1');
  const line0b = field('line-0-b', 'LINE_ITEMS:0');
  const line1b = field('line-1-b', 'LINE_ITEMS:1');
  const fields = [line0a, line1a, line0b, line1b];

  const result = selection.move(fields, [line0b], -1, scopeOf);

  assert.equal(result.moved, true);
  assert.deepEqual(
    result.fields.map(item => item.name),
    ['line-0-b', 'line-1-a', 'line-0-a', 'line-1-b'],
  );
  assert.equal(selection.canMove(result.fields, [line0b], -1, scopeOf), false);
});

test('a selected block can be dropped at an explicit insertion point', () => {
  const fields = [field('a'), field('b'), field('c'), field('d'), field('e')];
  const result = selection.moveTo(fields, [fields[1], fields[3]], fields[4], 'after', scopeOf);

  assert.equal(result.moved, true);
  assert.deepEqual(result.fields.map(item => item.name), ['a', 'c', 'e', 'b', 'd']);
});

test('dragging cannot move selected fields across structural scopes', () => {
  const headerA = field('header-a', 'HEADER');
  const headerB = field('header-b', 'HEADER');
  const footerA = field('footer-a', 'FOOTER');
  const fields = [headerA, footerA, headerB];
  const result = selection.moveTo(fields, [headerB], footerA, 'before', scopeOf);

  assert.equal(result.moved, false);
  assert.deepEqual(result.fields, fields);
});
