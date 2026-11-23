'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const start = source.indexOf('function quotationValidationIssues(');
const end = source.indexOf('\nfunction render(r)', start);
assert.ok(start >= 0 && end > start);

function element(tag) {
  return {
    tag, children: [], textContent: '', className: '', entries: [],
    appendChild(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  };
}

const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, element('div'));
  return nodes.get(id);
};
const visibility = new Map();
const context = vm.createContext({
  document: {
    getElementById: node,
    querySelector: selector => node(selector),
    createElement: element,
  },
  quotationField: value => value?.value ?? value ?? null,
  quotationSource: evidence => evidence?.page ? `p. ${evidence.page}` : null,
  confidenceLabel: value => value == null ? null : `${Math.round(value * 100)} %`,
  fmt: value => String(value),
  money: value => value == null ? null : `${Number(value).toFixed(2)} EUR`,
  unitMoney: value => value == null ? null : `${Number(value).toFixed(2)} EUR`,
  fillKv(container, entries) {
    container.entries = entries.filter(([, value]) => value != null && value !== '');
    container.children = container.entries.map(() => element('div'));
  },
  toggleSection: (id, visible) => visibility.set(id, visible),
  addTextCell(row, value) { row.appendChild({ value }); },
});
vm.runInContext(source.slice(start, end), context, { filename: 'web/app.js:renderQuotation' });

test('offers get dedicated summary, parties, article grid, subtotals and controls', () => {
  const quotation = {
    number: { value: 'GYo-181126-1509', confidence: .995 },
    date: { value: '2026-11-18' }, valid_until: { value: '2027-02-28' },
    project: { value: '47D GAMME CLIMATE - TERSUN' },
    seller: { name: 'elm.leblanc SAS', address: '124 rue de Stalingrad', siren: '542097944' },
    recipient: { name: { value: 'WENDEL AGEN' }, address: { value: '47550 BOE' } },
    project_details: { installer: { value: 'TERSUN' }, notes: ['GAMME CLIMATE 3000i'] },
    line_items: [{ line_number: 7, references: { article_number: '7733701806' },
      description: 'PAC Air/Air', product_family: 'Climate 6000i', quantity: 30,
      pricing: { unit_price: 156.89, eco_participation_unit: 1.75 },
      amounts: { net: 4706.78 }, evidence: { page: 2 } }],
    sections: [{ name: 'Climate 6000i', subtotal: 22247.1, evidence: { page: 2 } }],
    totals: { before_eco_participation: 75085.81, eco_participation: 873.1,
      total_net: 75958.91, currency: 'EUR' },
    validation: { status: 'WARNING', product_sum: 75085.84, section_sum: 75085.81,
      eco_sum: 873.1 },
    period: { kind: 'Marché', start: { value: '2026-11-18' }, end: { value: '2027-02-28' } },
    terms: { delivery: 'Livraison standard.' },
  };
  context.renderQuotation({}, quotation);
  assert.ok(node('quoteSummary').entries.some(([label, value]) =>
    label === 'N° d’offre' && value === 'GYo-181126-1509'));
  assert.equal(node('quoteParties').children.length, 2);
  assert.equal(node('#quoteLinesTable tbody').children.length, 1);
  assert.equal(node('#quoteLinesTable tbody').children[0].children[1].value, '7733701806');
  assert.equal(node('#quoteSectionsTable tbody').children.length, 1);
  assert.equal(node('quoteTerms').children[0].children[0].textContent, 'Livraison');
  assert.equal(visibility.get('secQuoteLines'), true);
  const issues = node('quoteValidation').children[1].children.map(child => child.textContent);
  assert.ok(issues.some(issue => issue.includes('Article 7') && issue.includes('0.08 EUR')));
  assert.ok(issues.some(issue => issue.includes('Somme des articles') && issue.includes('75085.84 EUR')));
});

test('quote-specific sections exist and do not masquerade as purchase-order sections', () => {
  for (const id of ['quoteSections', 'quoteSummary', 'quoteParties', 'quoteProject',
    'quoteLinesTable', 'quoteSectionsTable', 'quoteTotals', 'quoteValidation',
    'quotePeriod', 'quoteTerms', 'orderSections']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(source, /toggleSection\('orderSections', !isQuotation\)/);
  assert.match(source, /if \(isQuotation\) \{\s*renderQuotation\(r, quotation \|\| \{\}\);/);
});

test('blank offer fields remain blank and the warning identifies each missing value', () => {
  context.renderQuotation({}, {
    line_items: [
      { line_number: 1, references: { article_number: '7716704738' },
        description: 'Article gratuit', quantity: 1, pricing: { unit_price: 0 }, amounts: {} },
      { line_number: 2, references: { article_number: '7716704739' },
        description: 'Prix absent', quantity: 3, pricing: { eco_participation_unit: 1.5 }, amounts: {} },
    ],
    sections: [], totals: {}, validation: { status: 'WARNING' },
  });
  const rows = node('#quoteLinesTable tbody').children;
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children[5].value, '0.00 EUR');
  assert.equal(rows[0].children[7].value, null);
  assert.equal(rows[1].children[5].value, null);
  assert.equal(rows[1].children[6].value, '1.50 EUR');
  const issues = node('quoteValidation').children[1].children.map(child => child.textContent);
  assert.ok(issues.some(issue => issue.includes('7716704738') && issue.includes('montant HT') && !issue.includes('prix unitaire')));
  assert.ok(issues.some(issue => issue.includes('7716704739') && issue.includes('prix unitaire') && issue.includes('montant HT')));
});
