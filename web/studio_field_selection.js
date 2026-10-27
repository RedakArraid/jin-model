'use strict';

(function exposeFieldSelectionTools(root, factory) {
  const tools = factory();
  if (typeof module === 'object' && module.exports) module.exports = tools;
  if (root) root.JinStudioFieldSelection = tools;
})(typeof globalThis !== 'undefined' ? globalThis : this, () => {
  function selectedSet(selectedFields) {
    return selectedFields instanceof Set
      ? selectedFields
      : new Set(selectedFields || []);
  }

  function move(fields, selectedFields, direction, scopeOf) {
    const ordered = [...(fields || [])];
    const selected = selectedSet(selectedFields);
    const step = Number(direction) < 0 ? -1 : 1;
    const scopes = new Map();

    ordered.forEach((field, index) => {
      const scope = String(scopeOf(field, index));
      if (!scopes.has(scope)) scopes.set(scope, []);
      scopes.get(scope).push(index);
    });

    let moved = false;
    scopes.forEach(slots => {
      const scopedFields = slots.map(index => ordered[index]);
      const selectedPositions = scopedFields
        .map((field, position) => (selected.has(field) ? position : -1))
        .filter(position => position >= 0);
      if (!selectedPositions.length) return;

      const selectedInOrder = scopedFields.filter(field => selected.has(field));
      const remaining = scopedFields.filter(field => !selected.has(field));
      const insertionIndex = step < 0
        ? Math.max(0, Math.min(...selectedPositions) - 1)
        : Math.min(remaining.length, Math.max(...selectedPositions) + 2 - selectedInOrder.length);
      const reordered = [
        ...remaining.slice(0, insertionIndex),
        ...selectedInOrder,
        ...remaining.slice(insertionIndex),
      ];

      if (reordered.some((field, position) => field !== scopedFields[position])) moved = true;
      slots.forEach((fieldIndex, position) => { ordered[fieldIndex] = reordered[position]; });
    });

    return { fields: ordered, moved };
  }

  function canMove(fields, selectedFields, direction, scopeOf) {
    return move(fields, selectedFields, direction, scopeOf).moved;
  }

  function moveTo(fields, selectedFields, targetField, position, scopeOf) {
    const ordered = [...(fields || [])];
    const selected = selectedSet(selectedFields);
    if (!targetField || selected.has(targetField)) return { fields: ordered, moved: false };
    const targetScope = String(scopeOf(targetField));
    const slots = ordered
      .map((field, index) => ({ field, index }))
      .filter(item => String(scopeOf(item.field, item.index)) === targetScope)
      .map(item => item.index);
    const scopedFields = slots.map(index => ordered[index]);
    const selectedInOrder = scopedFields.filter(field => selected.has(field));
    if (!selectedInOrder.length) return { fields: ordered, moved: false };
    const remaining = scopedFields.filter(field => !selected.has(field));
    const targetIndex = remaining.indexOf(targetField);
    if (targetIndex < 0) return { fields: ordered, moved: false };
    const insertionIndex = targetIndex + (position === 'after' ? 1 : 0);
    const reordered = [
      ...remaining.slice(0, insertionIndex),
      ...selectedInOrder,
      ...remaining.slice(insertionIndex),
    ];
    const moved = reordered.some((field, index) => field !== scopedFields[index]);
    slots.forEach((fieldIndex, index) => { ordered[fieldIndex] = reordered[index]; });
    return { fields: ordered, moved };
  }

  return Object.freeze({ move, canMove, moveTo });
});
