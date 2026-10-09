'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function rawFieldDisplay(');
const end = source.indexOf('\n//', start);
assert.ok(start >= 0 && end > start, 'UI delivery-address selector must be present');
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context, { filename: 'web/app.js:uiDeliveryAddresses' });
const plain = value => JSON.parse(JSON.stringify(value));

test('UI displays delivery normalized_value verbatim before value', () => {
  const normalizedValue = 'GAZ SERVICE RAPIDE\n3 RUE DU PALATINAT\n78300 POISSY\nFrance';
  const payload = {
    normalized_output: { order: { delivery_address: {
      role: 'ship_to',
      party_name: 'GAZ SERVICE RAPIDE',
      normalized_value: normalizedValue,
      value: 'VALUE SHOULD NOT WIN',
    } } },
  };

  const addresses = plain(context.uiDeliveryAddresses(payload));

  assert.deepEqual(addresses[0].lines, [normalizedValue]);
  assert.equal(addresses[0].direct_value, true);
});

test('UI falls back to delivery value when normalized_value is absent', () => {
  const payload = {
    business_extractions: { purchase_order: { ship_to: {
      name: 'CLIENT',
      address: { value: 'CLIENT\n10 RUE TEST\n75001 PARIS' },
    } } },
  };

  const addresses = plain(context.uiDeliveryAddresses(payload));

  assert.deepEqual(addresses[0].lines, ['CLIENT\n10 RUE TEST\n75001 PARIS']);
  assert.equal(addresses[0].direct_value, true);
});

test('UI shows only the selected normalized delivery address', () => {
  const normalizedValue = 'Z.A. HENRI SPRIET\n1 RUE PHILIPPE LEBON\n14120 MONDEVILLE\nFRANCE';
  const payload = {
    schema_version: 'jin-clean-extraction-v2',
    order: { delivery_address: {
      role: 'ship_to',
      role_label: 'Adresse de livraison',
      party_name: 'Z.A. HENRI SPRIET',
      customer_agency_code: 'MON01',
      normalized_value: normalizedValue,
      value: '1 Rue Philippe Lebon, Z.A. HENRI SPRIET, 14120 MONDEVILLE',
      normalization: { status: 'STRUCTURED_UNVERIFIED' },
      verification: { status: 'STREET_MATCH_NUMBER_NOT_FOUND' },
    } },
    business_extractions: { purchase_order: { business_addresses: [
      { role: 'buyer', formatted_address: '[Le stock des *perts 124,126 RUE DE STALINGRAD' },
      { role: 'unknown', role_label: 'Superseded delivery candidate',
        formatted_address: 'www.piecesXpress.com | SAS au capital de 225 000' },
    ] } },
  };
  const addresses = plain(context.uiDeliveryAddresses(payload));
  assert.equal(addresses.length, 1);
  assert.equal(addresses[0].party_name, '');
  assert.deepEqual(addresses[0].lines, [normalizedValue]);
  assert.equal(addresses[0].selected, true);
  assert.equal(addresses[0].customer_agency_code, 'MON01');
  assert.doesNotMatch(JSON.stringify(addresses), /stock|capital|piecesXpress/i);
});

test('UI accepts the new value contract on a selected raw delivery role', () => {
  const payload = { business_extractions: { purchase_order: { business_addresses: [
    { role: 'buyer', formatted_address: 'ACHETEUR POLLUE' },
    { role: 'unknown', role_label: 'Superseded delivery candidate',
      clean_address: { lines: ['CANDIDAT INTERNE'] } },
    { role: 'ship_to', party_name: 'DEPOT TEST',
      value: 'DEPOT TEST\n2 RUE DE LA PAIX\n75000 PARIS\nFRANCE' },
  ] } } };
  const addresses = plain(context.uiDeliveryAddresses(payload));
  assert.equal(addresses.length, 1);
  assert.deepEqual(addresses[0].lines, ['DEPOT TEST\n2 RUE DE LA PAIX\n75000 PARIS\nFRANCE']);
  assert.equal(addresses[0].selected, true);
});

test('UI never falls back to an unclean raw formatted address', () => {
  const payload = { business_extractions: { purchase_order: { business_addresses: [
    { role: 'ship_to', formatted_address: '************************************************ ADHERENT' },
  ] } } };
  assert.deepEqual(plain(context.uiDeliveryAddresses(payload)), []);
});
