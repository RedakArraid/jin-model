'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const studioHtml = fs.readFileSync(path.join(__dirname, '../web/studio.html'), 'utf8');
const studioJs = fs.readFileSync(path.join(__dirname, '../web/studio.js'), 'utf8');
const studioCss = fs.readFileSync(path.join(__dirname, '../web/studio.css'), 'utf8');

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
  assert.match(html, /styles\.css\?v=5\.13\.3/);
  assert.match(html, /app\.js\?v=5\.16\.9/);
});

test('JIN Studio JavaScript only references DOM ids present in its page', () => {
  const ids = new Set([...studioHtml.matchAll(/id="([^"]+)"/g)].map(match => match[1]));
  const used = [...studioJs.matchAll(/\$\(['"]([^'"]+)['"]\)/g)].map(match => match[1]);
  const missing = [...new Set(used.filter(id => !ids.has(id)))];
  assert.deepEqual(missing, []);
});

test('JIN Studio exposes synchronized values, removable fields and coordinate-safe zoom', () => {
  for (const id of [
    'zoomOutButton', 'zoomInButton', 'fitWidthButton', 'focusFieldButton',
    'showOtherAnnotationsToggle', 'canvasStage', 'annotationJsonPreview', 'serviceTokenStatus',
    'rotateServiceTokenButton', 'serviceTokenResult', 'serviceTokenValue',
    'toggleServiceTokenButton', 'connectionBaseUrl', 'mcpConfigFormat',
    'mcpConfigLocation', 'mcpConnectionConfig', 'apiConnectionConfig', 'copyMcpConfigButton',
    'copyApiConfigButton',
  ]) {
    assert.match(studioHtml, new RegExp(`id="${id}"`));
  }
  assert.match(studioHtml, /document-type-select/);
  assert.match(studioHtml, /delete-field/);
  assert.match(studioHtml, /Que faut-il encadrer \?/);
  assert.match(studioHtml, /field-help/);
  assert.match(studioHtml, /field-example-value/);
  assert.match(studioJs, /catalogHelp/);
  assert.match(studioJs, /catalogExample/);
  assert.match(studioJs, /example\.textContent = catalogExample\(field\.label\)/);
  assert.match(studioJs, /helper\.addEventListener\('click', event => event\.stopPropagation\(\)\)/);
  assert.match(studioJs, /field\.path = availableCanonicalPath\(field\.label, field\.path, index\)/);
  assert.doesNotMatch(studioJs, /Le jeton est géré hors de JIN Studio/);
  assert.match(studioJs, /rotateServiceTokenFromUi/);
  assert.match(studioJs, /renderConnectionConfigs/);
  assert.match(studioJs, /\[mcp_servers\.jin_studio\]/);
  assert.match(studioJs, /http_headers/);
  assert.match(studioJs, /streamable-http/);
  assert.match(studioJs, /\/api\/studio/);
  assert.match(studioHtml, /studio\.js\?v=10/);
  assert.match(studioHtml, /studio\.css\?v=7/);
  assert.match(studioJs, /updateFieldFromRegion/);
  assert.match(studioJs, /value \/ \(index % 2 === 0 \? annotationCanvas\.width : annotationCanvas\.height\) \* 1000/);
  assert.match(studioCss, /\.canvas-stage \{ position: relative/);
  assert.match(studioJs, /if \(!state\.showOtherAnnotations && index !== state\.selectedField\) return/);
  assert.match(studioJs, /localStorage\.setItem\(/);
  assert.match(studioHtml, /Le cadre du champ sélectionné reste toujours visible/);
  assert.ok(
    studioHtml.indexOf('id="addFieldButton"') > studioHtml.indexOf('id="fieldList"'),
    'the add-field button must be rendered below the field list',
  );
});
