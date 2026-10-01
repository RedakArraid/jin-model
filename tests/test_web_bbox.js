'use strict';

// Run with: node --test tests/test_web_bbox.js
// Execute the actual UI functions, without starting the app or loading a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function collectBoxes(');
const end = source.indexOf('\nfunction render(', start);
assert.ok(start >= 0 && end > start, 'UI bounding-box functions must be present');

function element(tag) {
  return {
    tag, attributes: {}, children: [],
    setAttribute(name, value) { this.attributes[name] = String(value); },
    appendChild(child) { this.children.push(child); },
  };
}

const context = vm.createContext({ document: { createElementNS: (_, tag) => element(tag) } });
vm.runInContext(source.slice(start, end), context, { filename: 'web/app.js:collectBoxes' });
const { collectBoxes, drawBox } = context;
const plain = value => JSON.parse(JSON.stringify(value));

function orderPayload(width, height, bbox, page = 1) {
  return { business_extractions: { purchase_order: {
    pages: [{ page, width, height }],
    purchase_order: { number: { value: 'ORDER-12345', evidence: { page, bbox } } },
  } } };
}

function renderedRectangle(payload, page, width, height) {
  const boxes = collectBoxes(payload);
  const number = boxes[page].find(box => box.type === 'field');
  const svg = element('svg');
  drawBox(svg, number, width, height);
  return svg.children[0].attributes;
}

function near(actual, expected) {
  assert.ok(Math.abs(Number(actual) - expected) < 0.000001, `${actual} should equal ${expected}`);
}

test('native PDF order-number rectangle returns to its actual source location', () => {
  // Actual PDF point coordinates observed for the order-number token on 002135.
  const bbox = [124.35003662109375, 232.4409942626953, 200.2129669189453, 241.15359497070312];
  const payload = orderPayload(595, 841, bbox);
  const rectangle = renderedRectangle(payload, 1, 595, 841);
  near(rectangle.x, bbox[0]);
  near(rectangle.y, bbox[1]);
  near(rectangle.width, bbox[2] - bbox[0]);
  near(rectangle.height, bbox[3] - bbox[1]);
  const resized = renderedRectangle(payload, 1, 1190, 1682);
  near(resized.x, bbox[0] * 2);
  near(resized.y, bbox[1] * 2);
});

test('OCR boxes with unknown crop transforms are hidden even when dimensions exist', () => {
  for (const source_type of ['scan_ocr', 'image_ocr']) {
    const payload = orderPayload(2480, 3508, [248, 350.8, 496, 701.6]);
    payload.business_extractions.purchase_order.pages[0].source_type = source_type;
    const original = JSON.stringify(payload);
    assert.deepEqual(plain(collectBoxes(payload)), {});
    assert.equal(JSON.stringify(payload), original, 'OCR evidence stays unchanged in JSON');
  }
});

test('rotated core coordinates are hidden until an inverse transform is available', () => {
  const payload = orderPayload(595, 841, [100, 200, 150, 220]);
  payload.business_extractions.purchase_order.pages[0].rotation_applied = -1.25;
  assert.deepEqual(plain(collectBoxes(payload)), {});
});

test('weak boxes remain visible when OCR core coordinates cannot be reprojected', () => {
  const payload = orderPayload(2480, 3508, [248, 350.8, 496, 701.6]);
  const page = payload.business_extractions.purchase_order.pages[0];
  page.source_type = 'scan_ocr';
  payload.pages = [page];
  payload.blocks = [{ source: { page: 1, bbox: [248, 350.8, 496, 701.6] } }];
  payload.weak_field_suggestions = { spans: [
    { page: 1, bbox: [100, 200, 300, 400], label: 'ORDER_NUMBER' },
  ] };
  const boxes = collectBoxes(payload);
  assert.deepEqual(plain(boxes[1].map(box => box.type)), ['weak']);
  assert.deepEqual(plain(boxes[1][0].bbox), [100, 200, 300, 400]);
});

test('weak boxes stay normalized and do not require core page dimensions', () => {
  const payload = { weak_field_suggestions: { spans: [
    { page: 2, bbox: [100, 200, 300, 400], label: 'ORDER_NUMBER' },
  ] } };
  const boxes = collectBoxes(payload);
  assert.deepEqual(plain(boxes[2][0].bbox), [100, 200, 300, 400]);
  const svg = element('svg');
  drawBox(svg, boxes[2][0], 595, 842);
  near(svg.children[0].attributes.x, 59.5);
  near(svg.children[0].attributes.y, 168.4);
});

test('core boxes without dimensions or page provenance are omitted', () => {
  const payload = orderPayload(595, 841, [100, 200, 150, 220]);
  payload.business_extractions.purchase_order.pages = [];
  // Generic pages must not silently supply a different coordinate frame.
  payload.pages = [{ page: 1, width: 2480, height: 3508 }];
  assert.deepEqual(plain(collectBoxes(payload)), {});
  const missingPage = orderPayload(595, 841, [100, 200, 150, 220]);
  delete missingPage.business_extractions.purchase_order.purchase_order.number.evidence.page;
  assert.deepEqual(plain(collectBoxes(missingPage)), {});
});

test('page lookup uses page identifiers rather than array position', () => {
  const payload = orderPayload(1000, 2000, [100, 200, 200, 400], 3);
  payload.business_extractions.purchase_order.pages.unshift({ page: 1, width: 595, height: 842 });
  const boxes = collectBoxes(payload);
  assert.deepEqual(Object.keys(boxes), ['3']);
  assert.deepEqual(plain(boxes[3][0].bbox), [100, 100, 200, 200]);
});

test('addresses nested inside the purchase order are included once', () => {
  const payload = orderPayload(595, 841, [100, 200, 150, 220]);
  const address = { role: 'ship_to', evidence: { page: 1, bbox: [40, 300, 240, 380] } };
  payload.business_extractions.purchase_order.business_addresses = [address];
  payload.business_extractions.business_addresses = [];
  assert.equal(collectBoxes(payload)[1].filter(box => box.type === 'address').length, 1);
  payload.business_extractions.business_addresses = [address];
  assert.equal(collectBoxes(payload)[1].filter(box => box.type === 'address').length, 1);
});

test('address overlays exclude buyer and superseded internal candidates', () => {
  const payload = orderPayload(595, 841, [100, 200, 150, 220]);
  payload.business_extractions.purchase_order.business_addresses = [
    { role: 'buyer', evidence: { page: 1, bbox: [10, 250, 200, 280] } },
    { role: 'unknown', role_label: 'Superseded delivery candidate',
      evidence: { page: 1, bbox: [20, 290, 220, 320] } },
    { role: 'ship_to', evidence: { page: 1, bbox: [40, 330, 240, 380] } },
  ];
  const boxes = collectBoxes(payload)[1].filter(box => box.type === 'address');
  assert.equal(boxes.length, 1);
  assert.deepEqual(plain(boxes[0].bbox), [40 / 595 * 1000, 330 / 841 * 1000, 240 / 595 * 1000, 380 / 841 * 1000]);
});

test('generic blocks use their own declared page dimensions', () => {
  const payload = { pages: [{ page: 1, width: 600, height: 800 }], blocks: [
    { type: 'paragraph', source: { page: 1, bbox: [60, 80, 120, 160] } },
  ] };
  assert.deepEqual(plain(collectBoxes(payload)[1][0].bbox), [100, 100, 200, 200]);
});

test('invalid or out-of-page boxes do not produce SVG coordinates', () => {
  for (const bbox of [[100, 200, 90, 220], [NaN, 20, 30, 40], [0, 0, 596, 842], [0, -1, 30, 40]]) {
    assert.deepEqual(plain(collectBoxes(orderPayload(595, 841, bbox))), {});
  }
  const svg = element('svg');
  drawBox(svg, { bbox: [100, 200, 300, 400], type: 'field' }, NaN, 842);
  assert.equal(svg.children.length, 0);
});

test('PDF loading disables evaluation for uploaded documents', async () => {
  const renderStart = source.indexOf('async function renderPdf(');
  const renderEnd = source.indexOf('\n//', renderStart);
  let options;
  const pdfContext = vm.createContext({ pdfjsLib: {
    getDocument(received) {
      options = received;
      return { promise: Promise.reject(new Error('Stop before rendering')) };
    },
  } });
  vm.runInContext(source.slice(renderStart, renderEnd), pdfContext);
  await pdfContext.renderPdf({ objectUrl: 'blob:local-upload' }, 1);
  assert.equal(options.url, 'blob:local-upload');
  assert.equal(options.isEvalSupported, false);
});

test('PDF legend explains hidden OCR evidence and clears the note for native pages', async () => {
  const renderStart = source.indexOf('async function renderPdf(');
  const renderEnd = source.indexOf('\n//', renderStart);
  const elements = new Map();
  function domElement(tag) {
    const node = element(tag);
    const classes = new Set();
    node.classList = {
      toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
      contains(name) { return classes.has(name); },
    };
    node.appendChild = child => {
      node.children.push(child);
      if (child.id) elements.set(child.id, child);
    };
    return node;
  }
  const container = domElement('div');
  container.querySelectorAll = () => [];
  const legend = domElement('div');
  elements.set('pdfPages', container);
  elements.set('bboxLegend', legend);
  const pdfContext = vm.createContext({
    renderGen: 1,
    document: {
      getElementById: id => elements.get(id),
      createElement: domElement,
    },
  });
  vm.runInContext(source.slice(start, end) + '\n' + source.slice(renderStart, renderEnd), pdfContext);
  const entry = { pdfDoc: { numPages: 0 }, result: {
    pages: [{ page: 1, source_type: 'scan_ocr', width: 2480, height: 3508 }],
  } };
  await pdfContext.renderPdf(entry, 1);
  const note = elements.get('bboxOcrNote');
  assert.match(note.textContent, /OCR.*JSON/);
  assert.equal(note.classList.contains('hidden'), false);
  assert.equal(legend.classList.contains('hidden'), false);
  entry.result.pages[0].source_type = 'pdf_native';
  await pdfContext.renderPdf(entry, 1);
  assert.equal(note.classList.contains('hidden'), true);
  assert.equal(legend.classList.contains('hidden'), true);
  assert.equal(legend.children.length, 1, 'Repeated renders do not duplicate the note');
});
