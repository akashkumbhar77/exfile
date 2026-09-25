/**
 * ${enum_name} stages, in the order they sort: ${order_summary}.
 * Anything else (including blank) counts as unknown and sorts last.
 */
function ${fn}(value) {
  var v = normText_(value);
  if (v !== '') {
${branches}
  }
  return { value: null, order: ${unknown_order} };
}
