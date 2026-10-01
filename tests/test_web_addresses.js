'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function uiDeliveryAddresses(');
const end = source.indexOf('\n//', start);
assert.ok(start >= 0 && end > start, 'UI delivery-address selector must be present');
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context, { filename: 'web/app.js:uiDeliveryAddresses' });
const plain = value => JSON.parse(JSON.stringify(value));

test('UI shows only the selected normalized delivery address', () => {
  const payload = {
    normalized_output: { order: { delivery_address: {
      role: 'ship_to',
      role_label: 'Adresse de livraison',
      party_name: 'Z.A. HENRI SPRIET',
      formatted_lines: [
        'Z.A. HENRI SPRIET',
        '1 RUE PHILIPPE LEBON',
        '14120 MONDEVILLE',
        'FRANCE',
      ],
      normalization: { status: 'STRUCTURED_UNVERIFIED' },
      verification: { status: 'STREET_MATCH_NUMBER_NOT_FOUND' },
    } } },
    business_extractions: { purchase_order: { business_addresses: [
      { role: 'buyer', formatted_address: '[Le stock des *perts 124,126 RUE DE STALINGRAD' },
      { role: 'unknown', role_label: 'Superseded delivery candidate',
        formatted_address: 'www.piecesXpress.com | SAS au capital de 225 000' },
    ] } },
  };
  const addresses = plain(context.uiDeliveryAddresses(payload));
  assert.equal(addresses.length, 1);
  assert.equal(addresses[0].party_name, 'Z.A. HENRI SPRIET');
  assert.deepEqual(addresses[0].lines, [
    '1 RUE PHILIPPE LEBON', '14120 MONDEVILLE', 'FRANCE',
  ]);
  assert.equal(addresses[0].selected, true);
  assert.doesNotMatch(JSON.stringify(addresses), /stock|capital|piecesXpress/i);
});

test('UI fallback accepts only cleaned delivery roles', () => {
  const payload = { business_extractions: { purchase_order: { business_addresses: [
    { role: 'buyer', formatted_address: 'ACHETEUR POLLUE' },
    { role: 'unknown', role_label: 'Superseded delivery candidate',
      clean_address: { lines: ['CANDIDAT INTERNE'] } },
    { role: 'ship_to', party_name: 'DEPOT TEST',
      clean_address: { status: 'STRUCTURED_UNVERIFIED', lines: [
        'DEPOT TEST', '2 RUE DE LA PAIX', '75000 PARIS', 'FRANCE',
      ] } },
  ] } } };
  const addresses = plain(context.uiDeliveryAddresses(payload));
  assert.equal(addresses.length, 1);
  assert.deepEqual(addresses[0].lines, ['2 RUE DE LA PAIX', '75000 PARIS', 'FRANCE']);
  assert.equal(addresses[0].selected, false);
});

test('UI never falls back to an unclean raw formatted address', () => {
  const payload = { business_extractions: { purchase_order: { business_addresses: [
    { role: 'ship_to', formatted_address: '************************************************ ADHERENT' },
  ] } } };
  assert.deepEqual(plain(context.uiDeliveryAddresses(payload)), []);
});
