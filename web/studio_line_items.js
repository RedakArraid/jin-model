'use strict';

(function exposeLineItemTools(root, factory) {
  const tools = factory();
  if (typeof module === 'object' && module.exports) module.exports = tools;
  if (root) root.JinStudioLineItems = tools;
})(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  const PATH_SUFFIXES = Object.freeze({
    LINE_ITEM_NUMBER: 'line_number',
    LINE_ITEM_REFERENCE: 'references.article_number',
    LINE_ITEM_DESCRIPTION: 'description',
    LINE_ITEM_QUANTITY: 'quantity',
    LINE_ITEM_UNIT: 'unit',
    LINE_ITEM_UNIT_PRICE: 'pricing.net_unit_price',
    LINE_ITEM_TOTAL: 'amounts.total',
    LINE_ITEM_DELIVERY_DATE: 'delivery.date',
  });
  const FIELD_ORDER = Object.freeze(Object.keys(PATH_SUFFIXES));
  const CORE_FIELDS = Object.freeze([
    'LINE_ITEM_REFERENCE',
    'LINE_ITEM_DESCRIPTION',
    'LINE_ITEM_QUANTITY',
    'LINE_ITEM_UNIT',
    'LINE_ITEM_UNIT_PRICE',
    'LINE_ITEM_TOTAL',
  ]);
  const PREANNOTATION_FIELDS = Object.freeze([
    ['LINE_ITEM_NUMBER', ['line_number']],
    ['LINE_ITEM_REFERENCE', [
      'references.article_number', 'references.material_number',
      'references.supplier_material_number', 'article_number', 'material_number',
      'supplier_material_number', 'product_code',
    ]],
    ['LINE_ITEM_DESCRIPTION', ['description']],
    ['LINE_ITEM_QUANTITY', ['quantity']],
    ['LINE_ITEM_UNIT', ['unit', 'uom']],
    ['LINE_ITEM_UNIT_PRICE', [
      'pricing.net_unit_price', 'pricing.unit_price', 'net_unit_price', 'unit_price',
    ]],
    ['LINE_ITEM_TOTAL', ['amounts.total', 'amounts.net', 'line_total', 'total']],
    ['LINE_ITEM_DELIVERY_DATE', [
      'delivery.date', 'requested_delivery_date', 'delivery_date',
    ]],
  ]);
  const PATH_PATTERN = /(?:^|\.)(?:lines|line_items)\[(\d+)\](?:\.|$)/;
  const CHARGE_PATH_PATTERN = /(?:^|\.)additional_charges\[(\d+)\](?:\.|$)/;

  function isLineItemLabel(label) {
    return Object.prototype.hasOwnProperty.call(PATH_SUFFIXES, String(label || ''));
  }

  function indexFromPath(path) {
    const match = String(path || '').match(PATH_PATTERN);
    return match ? Number(match[1]) : -1;
  }

  function canonicalPath(label, index) {
    if (!isLineItemLabel(label)) return '';
    const normalizedIndex = Number(index);
    if (!Number.isInteger(normalizedIndex) || normalizedIndex < 0) return '';
    return `order.line_items[${normalizedIndex}].${PATH_SUFFIXES[label]}`;
  }

  function indices(fields) {
    return [...new Set((fields || [])
      .map(field => indexFromPath(field?.path))
      .filter(index => index >= 0))]
      .sort((left, right) => left - right);
  }

  function nextIndex(fields) {
    const found = indices(fields);
    return found.length ? found[found.length - 1] + 1 : 0;
  }

  function createField(label, index) {
    return {
      path: canonicalPath(label, index),
      label,
      status: 'UNREVIEWED',
      extracted_value: '',
      corrected_value: null,
      evidence: null,
      comment: '',
    };
  }

  function createLine(index, labels = CORE_FIELDS) {
    return labels.map(label => createField(label, index));
  }

  function semanticKey(field) {
    const label = String(field?.label || '');
    if (!label) return '';
    if (label.startsWith('ADDITIONAL_CHARGE_')) {
      const match = String(field?.path || '').match(CHARGE_PATH_PATTERN);
      return `${label}@charge-${match ? Number(match[1]) : 'unindexed'}`;
    }
    if (!isLineItemLabel(label)) return label;
    const lineIndex = indexFromPath(field?.path);
    return `${label}@${lineIndex >= 0 ? lineIndex : 'unindexed'}`;
  }

  function deduplicate(fields) {
    const seen = new Set();
    return (fields || []).filter(field => {
      const key = semanticKey(field);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function nestedValue(root, relativePaths, evidenceOf) {
    for (const relativePath of relativePaths) {
      let candidate = root;
      for (const component of relativePath.split('.')) {
        if (!candidate || typeof candidate !== 'object'
          || !Object.prototype.hasOwnProperty.call(candidate, component)) {
          candidate = undefined;
          break;
        }
        candidate = candidate[component];
      }
      if (candidate == null || candidate === '') continue;
      if (typeof candidate === 'object'
        && Object.prototype.hasOwnProperty.call(candidate, 'value')) {
        if (candidate.value == null || candidate.value === '') continue;
        return { relativePath, value: candidate.value, evidence: evidenceOf(candidate) };
      }
      return { relativePath, value: candidate, evidence: evidenceOf(candidate) };
    }
    return null;
  }

  function preannotate(sourceLines, evidenceOf = () => null) {
    const result = [];
    (sourceLines || []).forEach((line, index) => {
      if (!line || typeof line !== 'object') return;
      const lineEvidence = evidenceOf(line);
      for (const [label, paths] of PREANNOTATION_FIELDS) {
        const candidate = nestedValue(line, paths, evidenceOf);
        if (!candidate) continue;
        result.push({
          ...createField(label, index),
          path: `order.line_items[${index}].${candidate.relativePath}`,
          extracted_value: candidate.value,
          evidence: candidate.evidence || lineEvidence,
        });
      }
    });
    return result;
  }

  function lineItemsLast(fields) {
    const regularFields = [];
    const lineItemGroups = new Map();
    const unindexedLineItemFields = [];
    for (const field of fields || []) {
      const lineIndex = indexFromPath(field?.path);
      if (!isLineItemLabel(field?.label) && lineIndex < 0) {
        regularFields.push(field);
      } else if (lineIndex < 0) {
        unindexedLineItemFields.push(field);
      } else {
        if (!lineItemGroups.has(lineIndex)) lineItemGroups.set(lineIndex, []);
        lineItemGroups.get(lineIndex).push(field);
      }
    }
    const lineItemFields = [...lineItemGroups.keys()]
      .sort((left, right) => left - right)
      .flatMap(index => lineItemGroups.get(index));
    return [...regularFields, ...lineItemFields, ...unindexedLineItemFields];
  }

  function compact(fields) {
    const mapping = new Map(indices(fields).map((oldIndex, newIndex) => [oldIndex, newIndex]));
    const compacted = (fields || []).map(field => {
      const oldIndex = indexFromPath(field?.path);
      if (oldIndex < 0 || !mapping.has(oldIndex) || mapping.get(oldIndex) === oldIndex) return field;
      return {
        ...field,
        path: String(field.path).replace(
          /((?:lines|line_items)\[)\d+(\])/,
          `$1${mapping.get(oldIndex)}$2`,
        ),
      };
    });
    return lineItemsLast(compacted);
  }

  function validate(fields) {
    const errors = [];
    const paths = new Set();
    const semanticFields = new Map();
    for (const field of fields || []) {
      const path = String(field?.path || '');
      if (paths.has(path)) errors.push(`Chemin duplique : ${path}`);
      paths.add(path);
      const key = semanticKey(field);
      if (key && semanticFields.has(key)) {
        const lineIndex = indexFromPath(path);
        errors.push(lineIndex >= 0
          ? `Le champ ${field.label} est present plusieurs fois dans la ligne ${lineIndex + 1}.`
          : `Le champ ${field.label} est present plusieurs fois.`);
      } else if (key) {
        semanticFields.set(key, field);
      }
      if (isLineItemLabel(field?.label) && indexFromPath(path) < 0) {
        errors.push(`Le champ ${field.label} doit appartenir a order.line_items[index].`);
      }
      if (String(field?.label || '').startsWith('ADDITIONAL_CHARGE_')
        && !CHARGE_PATH_PATTERN.test(path)) {
        errors.push(`Le champ ${field.label} doit appartenir a order.additional_charges[index].`);
      }
    }
    const found = indices(fields);
    found.forEach((value, position) => {
      if (value !== position) errors.push(
        `Les lignes doivent etre continues : line_items[${position}] est absent.`,
      );
    });
    return errors;
  }

  return Object.freeze({
    PATH_SUFFIXES,
    FIELD_ORDER,
    CORE_FIELDS,
    isLineItemLabel,
    indexFromPath,
    canonicalPath,
    indices,
    nextIndex,
    createField,
    createLine,
    semanticKey,
    deduplicate,
    preannotate,
    lineItemsLast,
    compact,
    validate,
  });
});
